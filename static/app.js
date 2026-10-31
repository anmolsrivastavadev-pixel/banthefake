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
const VERDICT_CLASS = { ai: 'warn', edited: 'caution', camera: 'ok', unknown: '' };
const SCAN_ERRORS = {
  413: ['File too large', 'This file is over the upload limit. Try a smaller file or a shorter clip.'],
  415: ['Unsupported file', 'We can only scan JPG, PNG, WEBP, GIF, MP4, MOV and WEBM files.'],
  429: ['Slow down', 'Too many scans from your connection. Please wait a minute and try again.'],
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
  dropInner.replaceChildren(isImage ? el('img', { src: previewUrl, alt: '' }) : el('video', { src: previewUrl, controls: '', muted: '' }));
  setVerdict('loading', 'Scanning…', file.name);
  signalsEl.replaceChildren();
  detailsEl.replaceChildren();
  elaWrap.style.display = 'none';

  if (isImage) runELA(previewUrl);

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
    const [title, detail] = SCAN_ERRORS[res.status] || ['Scan failed', data.error || 'Something went wrong. Please try again.'];
    setVerdict('caution', title, detail);
    return;
  }
  renderReport(data);
}

function renderReport(r) {
  setVerdict(VERDICT_CLASS[r.verdict] ?? '', r.verdict_title, r.verdict_detail);
  signalsEl.replaceChildren(...(r.signals || []).map(s => {
    const [cls, label] = TAGS[s.kind] || TAGS.info;
    return el('li', {}, el('span', { class: `tag ${cls}` }, label), el('span', {}, s.text));
  }));

  const blocks = [];
  if (r.c2pa) {
    const c = r.c2pa;
    const rows = [
      ['Signature', c.valid ? 'Valid' : 'Invalid or untrusted'],
      ['AI-generated', c.ai_generated ? 'Yes, declared in credentials' : 'Not declared'],
      ['Made with', c.generator],
      ['Signed by', c.issuer],
      ['Actions', (c.actions || []).join(', ')],
    ];
    blocks.push(block('Content credentials (C2PA)', rows));
  }
  if (r.model) {
    const m = r.model;
    blocks.push(block('AI detector model', [
      ['Model', m.name],
      ['AI probability', `${Math.round(m.ai_probability * 100)}%`],
      ['Frames checked', String(m.frames_checked)],
    ]));
  }
  if (r.file) {
    const f = r.file;
    blocks.push(block('File', [
      ['Type', f.mime],
      ['Size', formatSize(f.size_bytes)],
      ['Dimensions', f.width && f.height ? `${f.width} × ${f.height}` : ''],
    ]));
  }
  detailsEl.replaceChildren(...blocks);
}

function block(title, rows) {
  const dl = el('dl');
  for (const [k, v] of rows) if (v) dl.append(el('dt', {}, k), el('dd', {}, v));
  return el('div', { class: 'block' }, el('h4', {}, title), dl);
}

function setVerdict(cls, title, body) {
  verdict.className = 'verdict ' + cls;
  verdict.replaceChildren(el('span', { class: 'dot' }), el('div', {}, el('b', {}, title), el('span', { class: 'muted' }, body)));
}

function runELA(url) {
  const img = new Image();
  img.onload = () => {
    const max = 900, s = Math.min(1, max / Math.max(img.width, img.height));
    const w = Math.round(img.width * s), h = Math.round(img.height * s);
    const a = document.createElement('canvas'); a.width = w; a.height = h;
    const actx = a.getContext('2d'); actx.drawImage(img, 0, 0, w, h);
    const orig = actx.getImageData(0, 0, w, h);
    const re = new Image();
    re.onload = () => {
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
document.getElementById('copy').addEventListener('click', async e => {
  await navigator.clipboard.writeText(letterEl.value);
  e.target.textContent = 'Copied ✓';
  setTimeout(() => e.target.textContent = 'Copy letter', 1500);
});
renderLetter();
