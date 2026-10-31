'use strict';

// ---------- Helpers ----------
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

// ---------- Scanner ----------
const drop = document.getElementById('drop');
const fileInput = document.getElementById('file');
const dropInner = document.getElementById('dropInner');
const verdict = document.getElementById('verdict');
const signalsEl = document.getElementById('signals');
const detailsEl = document.getElementById('details');
const elaWrap = document.getElementById('elaWrap');
const scanStatus = document.getElementById('scanStatus');

['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => { if (e.dataTransfer.files[0]) scan(e.dataTransfer.files[0]); });
fileInput.addEventListener('change', () => { if (fileInput.files[0]) scan(fileInput.files[0]); });

const TAGS = {
  ai: ['ai', 'AI'],
  edit: ['edit', 'EDITED'],
  camera: ['cam', 'CAMERA'],
  c2pa: ['c2pa', 'C2PA'],
  model: ['model', 'MODEL'],
  info: ['info', 'INFO'],
};
// Camera data is not proof a file is real, so that verdict is styled neutrally, never as a green "all clear".
const VERDICT_CLASS = {
  verified_capture: 'verified',
  verified_ai: 'warn',
  ai: 'warn',
  edited: 'caution',
  camera: 'neutral',
  unknown: '',
};
const SCAN_ERRORS = {
  413: ['File too large', 'This file is over the upload limit. Try a smaller file or a shorter clip.'],
  415: ['Unsupported file', 'We can only scan JPG, PNG, WEBP, GIF, MP4, MOV and WEBM files.'],
  429: ['Slow down', 'Too many scans from your connection. Please wait a minute and try again.'],
  503: ['Scanner busy', 'The scanner is handling other files right now. Please try again in a moment.'],
};

let scanSeq = 0;
let previewUrl = null;

function formatSize(bytes) {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(2)} MB` : `${(bytes / 1024).toFixed(1)} KB`;
}

async function scan(file) {
  const seq = ++scanSeq;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = URL.createObjectURL(file);
  const isImage = file.type.startsWith('image/');
  dropInner.replaceChildren(isImage
    ? el('img', { src: previewUrl, alt: `Preview of ${file.name}` })
    : el('video', { src: previewUrl, controls: '', muted: '', 'aria-label': `Preview of ${file.name}` }));
  // The preview replaced the visible label text, so keep the file input named for screen readers.
  fileInput.setAttribute('aria-label', 'Choose another image or video to scan');
  setVerdict('loading', 'Scanning…', file.name);
  signalsEl.replaceChildren();
  detailsEl.replaceChildren();
  elaWrap.style.display = 'none';

  if (isImage) runELA(previewUrl, seq);

  const body = new FormData();
  body.append('file', file);
  let res, data;
  try {
    res = await fetch('/api/scan', { method: 'POST', body });
    data = await res.json().catch(() => ({}));
  } catch {
    if (seq === scanSeq) setVerdict('caution', 'Could not reach the scanner', 'Check your connection and try again.');
    return;
  }
  if (seq !== scanSeq) return;
  if (!res.ok) {
    let [title, detail] = SCAN_ERRORS[res.status] || ['Scan failed', data.error || 'Something went wrong. Please try again.'];
    if (res.status === 415 && data.error) detail = data.error;
    setVerdict('caution', title, detail);
    return;
  }
  renderReport(data);
  const n = (data.signals || []).length;
  announce(`Scan finished. ${data.verdict_title}. ${data.verdict_detail || ''} ${n} ${n === 1 ? 'signal' : 'signals'} listed below.`);
}

function renderReport(r) {
  setVerdict(VERDICT_CLASS[r.verdict] ?? '', r.verdict_title, r.verdict_detail, false);
  signalsEl.replaceChildren(...(r.signals || []).map(s => {
    const [cls, label] = TAGS[s.kind] || TAGS.info;
    return el('li', {}, el('span', { class: `tag ${cls}` }, label), el('span', {}, s.text));
  }));

  const blocks = [];
  if (r.c2pa) blocks.push(c2paBlock(r.c2pa));
  if (r.model) blocks.push(modelBlock(r.model));
  if (r.faceswap) blocks.push(faceSwapBlock(r.faceswap));
  if (r.file) {
    const f = r.file;
    blocks.push(block('File', [
      ['Type', f.mime],
      ['Size', formatSize(f.size_bytes)],
      ['Dimensions', f.width && f.height ? `${f.width} × ${f.height}` : ''],
      ['Duration', f.duration_s ? `${f.duration_s.toFixed(1)} s` : ''],
    ]));
  }
  detailsEl.replaceChildren(...blocks);
}

function c2paBlock(c) {
  if (!c.generator && !c.issuer && !(c.actions || []).length) {
    return block('Content credentials (C2PA)', [['Status', 'Found, but could not be read']]);
  }
  let signature = 'Does NOT validate: the file may have been changed after signing';
  if (c.valid && c.trusted) signature = 'Valid, signer is on the trust list';
  else if (c.valid) signature = 'Intact, but the signer is not on our trust list';
  // Only a valid manifest from a trusted signer is evidence; anything else is shown as an unverified claim.
  const verified = Boolean(c.valid && c.trusted);
  const claim = v => (v && !verified ? `Claimed (not verified): ${v}` : v);
  const actions = (c.actions || []).map(a => a.replace(/^c2pa\./, '').replace(/_/g, ' '));
  const edits = (c.edits || []).map(a => a.replace(/^c2pa\./, '').replace(/_/g, ' '));
  const ingredients = (c.ingredients || []).map(i => [i.title, i.format, i.relationship, i.ai_generated ? 'declares generative AI' : ''].filter(Boolean).join(' · '));
  let aiRow = 'Not declared';
  if (c.ai_generated) aiRow = claim(c.ingredient_ai ? 'Yes, declared by an ingredient\'s own credentials' : 'Yes, declared in the credentials');
  let captureRow = 'Not declared';
  if (c.capture && verified) captureRow = 'Yes, declared as a camera capture';
  else if (c.claimed_capture || c.capture) captureRow = 'Claimed (not verified): declared as a camera capture';
  let signedAt = c.signed_at;
  if (signedAt) {
    const d = new Date(signedAt);
    if (!Number.isNaN(d.getTime())) signedAt = d.toLocaleString();
  }
  const b = block('Content credentials (C2PA)', [
    ['AI-generated', aiRow],
    ['Capture evidence', captureRow],
    ['Made with', claim(c.generator)],
    ['Signed by', claim(c.signer || c.issuer)],
    ['Signed at', claim(signedAt)],
    ['Signature', signature],
    ['Trust list', c.trust_list_version],
    ['Actions', actions.join(', ')],
    ['Recorded edits', edits.join(', ')],
    ['Ingredients', ingredients.join('; ')],
  ]);
  if (c.ai_generated) b.classList.add('flag');
  b.append(el('p', { class: 'note' }, 'Check the full history at contentcredentials.org/verify.'));
  return b;
}

function pct(p) {
  return Math.max(1, Math.min(99, Math.round(p * 100)));
}

function meterClass(p) {
  return p >= 0.85 ? 'high' : p >= 0.5 ? 'mid' : 'low';
}

function meter(p, label) {
  const fill = el('i', { class: meterClass(p) });
  fill.style.width = `${pct(p)}%`;
  return el('div', { class: 'meter', role: 'meter', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct(p)), 'aria-label': label }, fill);
}

const DECISION_READING = {
  likely: 'Likely AI-generated',
  possible: 'Possible signs of AI',
  none: 'No strong AI signal',
};

// Black-and-white and low-colour images are outside the detectors' reliable range: neutral for every tier.
const LOW_COLOUR_READING = 'Not reliable for black-and-white or low-colour images (scores shown for reference only)';

function modelBlock(m) {
  const p = m.ai_probability;
  const models = Array.isArray(m.models) && m.models.length ? m.models : [{ name: m.name, ai_probability: p }];
  let reading = DECISION_READING[m.decision] || '';
  if (m.low_colour) reading = LOW_COLOUR_READING;
  else if (m.decision === 'none' && (m.frames_likely || m.frames_possible)) reading = 'No consistent AI signal across the clip';
  else if (m.decision === 'possible' && m.frames_likely) reading = `Possible signs of AI: ${m.frames_likely} of ${m.frames_checked} frames rated likely AI-generated`;
  const b = block('AI detector models', [
    ['Setup', m.name],
    ['Combined reading', reading],
    ...models.map(s => [s.name, `${pct(s.ai_probability)}% chance of AI`]),
    ['Frames checked', m.frames_checked > 1 ? String(m.frames_checked) : ''],
    ['Video covered', m.coverage ? `${m.coverage.first_s.toFixed(1)} s to ${m.coverage.last_s.toFixed(1)} s${m.coverage.duration_s ? ` of ${m.coverage.duration_s.toFixed(1)} s` : ''}` : ''],
  ]);
  for (const s of models) b.append(meter(s.ai_probability, `${s.name}: estimated chance this is AI-generated`));
  if (Array.isArray(m.frame_probabilities) && m.frame_probabilities.length > 1) {
    const strip = el('div', { class: 'frames', role: 'img', 'aria-label': `Per-frame estimates from ${models[0].name}: ${m.frame_probabilities.map(fp => `${pct(fp)}%`).join(', ')}` });
    m.frame_probabilities.forEach((fp, i) => {
      const bar = el('i', { class: meterClass(fp), title: `Frame ${i + 1}: ${pct(fp)}%` });
      bar.style.height = `${pct(fp)}%`;
      strip.append(el('span', {}, bar));
    });
    b.append(strip);
  }
  if (models.length > 1) {
    b.append(el('p', { class: 'note' }, '"Likely AI-generated" needs both models to agree; one model on its own can only show possible signs.'));
  }
  b.append(el('p', { class: 'note' }, 'Estimates from machine-learning models. They can be wrong both ways, and low scores do not mean the file is real.'));
  return b;
}

function faceSwapBlock(f) {
  let b;
  if (f.status !== 'ok') {
    b = block('Face-swap check', [
      ['Status', 'Unavailable'],
      ['Reason', f.reason || 'The experimental backend could not run'],
    ]);
  } else {
    const faces = Array.isArray(f.faces) ? f.faces : [];
    let reading = 'No faces were large and confident enough to analyze';
    if (faces.length) reading = f.flagged ? 'One or more faces were flagged' : 'Below threshold — this check misses many face swaps.';
    const rows = [
      ['Reading', reading],
      ['Model', f.model_version],
      ['Threshold', `${pct(f.threshold)}%`],
      ['Frames checked', f.frames_checked > 1 ? String(f.frames_checked) : ''],
      ['Candidates skipped', f.faces_skipped ? String(f.faces_skipped) : ''],
      ['Faces omitted by limit', f.faces_omitted ? String(f.faces_omitted) : ''],
      ['Faces skipped at deadline', f.faces_timed_out ? String(f.faces_timed_out) : ''],
      ...faces.map((face, i) => {
        const where = Number.isInteger(face.frame)
          ? ` · frame ${face.frame + 1}${Number.isFinite(face.timestamp_s) ? ` at ${face.timestamp_s.toFixed(1)} s` : ''}`
          : '';
        const box = Array.isArray(face.box) ? ` · box ${face.box.join(', ')}` : '';
        return [`Face ${i + 1}`, `${pct(face.score)}% manipulation score${where}${box}`];
      }),
    ];
    b = block('Face-swap check', rows);
    faces.forEach((face, i) => b.append(meter(face.score, `Face ${i + 1}: experimental manipulation score`)));
    b.append(el('p', { class: 'note' }, 'Experimental and informational only. It is often wrong, misses many face swaps, and never changes the verdict.'));
  }
  b.classList.add('faceswap-block');
  const heading = b.querySelector('h3');
  heading.append(el('span', { class: 'experimental-badge' }, 'Experimental'));
  return b;
}

function block(title, rows) {
  const dl = el('dl');
  for (const [k, v] of rows) if (v) dl.append(el('dt', {}, k), el('dd', {}, v));
  return el('div', { class: 'block' }, el('h3', {}, title), dl);
}

function setVerdict(cls, title, body, speak = true) {
  verdict.className = 'verdict ' + cls;
  verdict.replaceChildren(el('span', { class: 'dot' }), el('div', {}, el('b', {}, title), el('span', { class: 'muted' }, body)));
  if (speak) announce(`${title}. ${body}`);
}

// Polite live region: screen readers hear scan progress and results without losing their place.
function announce(text) {
  scanStatus.textContent = '';
  setTimeout(() => { scanStatus.textContent = text; }, 50);
}

// `seq` is the scan this heatmap belongs to: a newer file chosen while it is being made must not get it.
function runELA(url, seq) {
  const img = new Image();
  img.onload = () => {
    if (seq !== scanSeq) return;
    const max = 900, s = Math.min(1, max / Math.max(img.width, img.height));
    const w = Math.round(img.width * s), h = Math.round(img.height * s);
    const a = document.createElement('canvas'); a.width = w; a.height = h;
    const actx = a.getContext('2d'); actx.drawImage(img, 0, 0, w, h);
    const orig = actx.getImageData(0, 0, w, h);
    const re = new Image();
    re.onload = () => {
      if (seq !== scanSeq) return;
      const b = document.createElement('canvas'); b.width = w; b.height = h;
      const bctx = b.getContext('2d'); bctx.drawImage(re, 0, 0);
      const comp = bctx.getImageData(0, 0, w, h);
      const out = document.getElementById('ela'); out.width = w; out.height = h;
      const octx = out.getContext('2d');
      const res = octx.createImageData(w, h);
      const scale = 20;
      for (let i = 0; i < orig.data.length; i += 4) {
        res.data[i] = Math.min(255, Math.abs(orig.data[i] - comp.data[i]) * scale);
        res.data[i + 1] = Math.min(255, Math.abs(orig.data[i + 1] - comp.data[i + 1]) * scale);
        res.data[i + 2] = Math.min(255, Math.abs(orig.data[i + 2] - comp.data[i + 2]) * scale);
        res.data[i + 3] = 255;
      }
      octx.putImageData(res, 0, 0);
      elaWrap.style.display = 'block';
    };
    re.src = a.toDataURL('image/jpeg', 0.9);
  };
  img.src = url;
}

// ---------- Petition ----------
const countEl = document.getElementById('count');
const goalEl = document.getElementById('goal');
const bar = document.getElementById('bar');
const thanks = document.getElementById('thanks');
const petition = document.getElementById('petition');

async function loadCount() {
  try {
    const res = await fetch('/api/petition/count');
    if (!res.ok) return;
    const { count, goal } = await res.json();
    countEl.textContent = count.toLocaleString();
    goalEl.textContent = goal.toLocaleString();
    bar.style.width = Math.min(100, (count / goal) * 100) + '%';
  } catch { /* keep last shown count */ }
}

function say(text, isError = false) {
  thanks.textContent = text;
  thanks.classList.toggle('err', isError);
}

petition.addEventListener('submit', async e => {
  e.preventDefault();
  const form = new FormData(petition);
  const payload = {
    name: (form.get('name') || '').trim(),
    email: (form.get('email') || '').trim(),
    location: (form.get('location') || '').trim(),
    website: form.get('website') || '',
    consent: form.get('consent') === 'on',
  };
  const button = petition.querySelector('button[type="submit"]');
  button.disabled = true;
  say('');
  try {
    const res = await fetch('/api/petition', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.status === 201) {
      petition.reset();
      say('Signed. Thank you! Now send a letter →');
      loadCount();
    } else if (res.status === 409) {
      say("You've already signed. Thank you!");
    } else if (res.status === 429) {
      say('Too many attempts from your connection. Please try again later.', true);
    } else {
      const data = await res.json().catch(() => ({}));
      say(data.error || 'Something went wrong. Please try again.', true);
    }
  } catch {
    say('Could not reach the server. Check your connection and try again.', true);
  } finally {
    button.disabled = false;
  }
});
loadCount();

// ---------- Letter generator ----------
const letterEl = document.getElementById('letter');
const LETTERS = {
  rep: (n, p) => `Dear Representative,

I'm ${n || '[your name]'}, a constituent from ${p || '[your town]'}. I'm writing to ask you to support strong laws against harmful deepfakes.

AI tools can now copy anyone's face or voice in minutes. They are being used to make non-consensual sexual images, run voice-clone scams, and spread fake videos of candidates. Victims often have no way to get the content removed.

I'm asking you to support legislation that:
1. Makes it illegal to create or share sexual or defamatory deepfakes of a real person without consent, and lets victims sue
2. Requires platforms to remove reported deepfakes within 48 hours
3. Requires AI-generated media to be clearly labeled
4. Bans apps designed to "undress" people or clone voices without consent

Please let me know where you stand on this issue.

Sincerely,
${n || '[your name]'}`,
  platform: (n, p) => `To the Trust & Safety team,

I'm ${n || '[your name]'}, a user from ${p || '[your location]'}. I'm asking you to do more to stop harmful deepfakes on your platform.

Specifically, please:
1. Remove reported non-consensual deepfakes within 48 hours and block re-uploads using hash-matching (e.g. StopNCII)
2. Read and show C2PA content credentials on uploaded media
3. Clearly label AI-generated images, audio and video
4. Ban ads and accounts that promote "undress" or face-swap apps

People's faces and voices should not be used against them without consent.

Thank you,
${n || '[your name]'}`,
  school: (n, p) => `Dear Administration,

I'm ${n || '[your name]'}, a member of our community in ${p || '[your town]'}. Deepfake "nude" images of students are a growing problem in schools, and I'm asking our school to act before it happens here.

Please consider:
1. A clear policy that creating or sharing deepfakes of students or staff is a serious disciplinary offense
2. A confidential way to report it, and a plan to support victims
3. Teaching students about consent, deepfakes, and how to report them (StopNCII.org, Take It Down)

Thank you for keeping our students safe.

Sincerely,
${n || '[your name]'}`,
};
function renderLetter() {
  letterEl.value = LETTERS[document.getElementById('recipient').value](
    document.getElementById('lname').value.trim(),
    document.getElementById('lplace').value.trim()
  );
}
['recipient', 'lname', 'lplace'].forEach(id => document.getElementById(id).addEventListener('input', renderLetter));
const copyStatus = document.getElementById('copyStatus');
document.getElementById('copy').addEventListener('click', async e => {
  const button = e.currentTarget;
  try {
    await navigator.clipboard.writeText(letterEl.value);
    button.textContent = 'Copied ✓';
    copyStatus.textContent = 'Letter copied to the clipboard.';
  } catch {
    letterEl.select();
    copyStatus.textContent = 'Could not copy automatically. The letter is selected: press Ctrl+C or Cmd+C to copy it.';
  }
  setTimeout(() => { button.textContent = 'Copy letter'; copyStatus.textContent = ''; }, 3000);
});
renderLetter();
