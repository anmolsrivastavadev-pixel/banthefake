// Pure logic shared by the background worker, the result window and the options page.
// No browser APIs here (except in pageFetchImage, which runs in the page): unit-tested with `node --test`.

// Placeholder: the extension does nothing until the user sets their own BanTheFake server in the options.
export const DEFAULT_SERVER = 'https://YOUR-DOMAIN.example';
// Same as the server's default BTF_MAX_UPLOAD_MB (50). The server enforces its own limit too (413).
export const MAX_IMAGE_BYTES = 50 * 1024 * 1024;
export const NO_PROOF = 'No tool can prove a file is real.';
// Video checks: frames captured from the playing <video> by capture.js. The server accepts up to 16 frames, 4 MB
// each and 16 MB in total (POST /api/scan-frames); the extension sends at most 8 frames and about 12 MB.
export const MAX_VIDEO_FRAMES = 8;
export const MAX_VIDEO_PAYLOAD_BYTES = 12 * 1024 * 1024;
// Players whose video sits under overlays, so a right-click there opens the "page" menu, not the "video" one.
// The page-level entry only appears on these sites (a documentUrlPatterns filter needs no host permission).
export const VIDEO_PAGE_PATTERNS = ['https://*.youtube.com/*', 'https://*.tiktok.com/*'];
const LABEL_PLATFORMS = ['youtube.com', 'tiktok.com'];

const LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1']);
// RFC 2606 / RFC 6761 names that can never be a real BanTheFake deployment.
const RESERVED_HOST = /(^|\.)(example(\.(com|org|net))?|invalid|test)$/i;

/**
 * Validate and normalize a BanTheFake server URL typed by the user.
 * https:// only, except http://localhost and http://127.0.0.1 for development.
 * Returns { ok: true, base } (no trailing slash; may include a path prefix) or { ok: false, error }.
 */
export function validateServerUrl(input) {
  const raw = String(input ?? '').trim();
  if (!raw) return { ok: false, error: 'Enter the address of a BanTheFake server.' };
  if (/[\s<>"'\\]/.test(raw)) return { ok: false, error: 'The address must not contain spaces, quotes or angle brackets.' };
  let url;
  try {
    url = new URL(raw);
  } catch {
    return { ok: false, error: 'That is not a valid address. Example: https://banthefake.your-domain.org' };
  }
  if (url.username || url.password) return { ok: false, error: 'The address must not contain a user name or password.' };
  if (url.search || url.hash || raw.includes('?') || raw.includes('#')) {
    return { ok: false, error: 'The address must not contain a query (?) or fragment (#).' };
  }
  if (url.protocol === 'http:') {
    if (!LOOPBACK_HOSTS.has(url.hostname)) {
      return { ok: false, error: 'Use https://. Plain http:// is only allowed for localhost (development).' };
    }
  } else if (url.protocol !== 'https:') {
    return { ok: false, error: 'Use an https:// address.' };
  }
  if (!url.hostname) return { ok: false, error: 'The address needs a host name.' };
  const path = url.pathname.replace(/\/+$/, '');
  return { ok: true, base: `${url.origin}${path}`, origin: url.origin };
}

/** True when the configured value is a usable, non-placeholder server. */
export function isConfigured(server) {
  const v = validateServerUrl(server);
  if (!v.ok) return false;
  return !RESERVED_HOST.test(new URL(v.base).hostname);
}

/** Host-permission match pattern for exactly the server's origin (ports are ignored by match patterns). */
export function originPattern(server) {
  const v = validateServerUrl(server);
  if (!v.ok) return null;
  const u = new URL(v.base);
  return `${u.protocol}//${u.hostname}/*`;
}

export function scanEndpoint(base) {
  return `${base}/api/scan`;
}

export function framesEndpoint(base) {
  return `${base}/api/scan-frames`;
}

export function healthEndpoint(base) {
  return `${base}/api/health`;
}

/** The site's "Get it taken down" section on the configured server. */
export function helpUrl(base) {
  return `${base}/#report`;
}

/** A short, safe upload filename from the image URL (the server sniffs the bytes; the name is informational). */
export function filenameFromUrl(src) {
  try {
    const u = new URL(src);
    if (u.protocol === 'data:' || u.protocol === 'blob:') return 'image';
    const last = decodeURIComponent(u.pathname.split('/').pop() || '');
    const clean = last.replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 100);
    return clean || 'image';
  } catch {
    return 'image';
  }
}

/** Where an image may be fetched from by the extension itself (the in-page fetch is tried first). */
export function extensionFetchAllowed(src) {
  try {
    const p = new URL(src).protocol;
    return p === 'https:' || p === 'data:';
  } catch {
    return false;
  }
}

/** Decode the base64 payload returned by pageFetchImage. */
export function base64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/**
 * Injected into the page (chrome.scripting.executeScript `func`), so it must be self-contained.
 * Fetches the image the user right-clicked with the page's own origin, so the browser's same-origin and
 * CORS rules apply exactly as they do for the page's scripts. Prefers the copy already in the HTTP cache.
 */
export async function pageFetchImage(src, maxBytes) {
  try {
    const res = await fetch(src, { cache: 'force-cache', credentials: 'same-origin', redirect: 'follow' });
    if (!res.ok) return { ok: false, error: `http_${res.status}` };
    const declared = Number(res.headers.get('content-length') || 0);
    if (declared > maxBytes) return { ok: false, error: 'too_large' };
    const buf = new Uint8Array(await res.arrayBuffer());
    if (buf.length > maxBytes) return { ok: false, error: 'too_large' };
    if (buf.length === 0) return { ok: false, error: 'empty' };
    let bin = '';
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return { ok: true, b64: btoa(bin), type: res.headers.get('content-type') || '', size: buf.length };
  } catch {
    return { ok: false, error: 'blocked' };
  }
}

// ---------- Verdict rendering (view model; the DOM is built from this with textContent only) ----------

// Camera data is not proof a file is real, so that verdict is styled neutrally, never as a green "all clear".
export const VERDICT_CLASS = {
  verified_capture: 'verified',
  verified_ai: 'warn',
  ai: 'warn',
  edited: 'caution',
  camera: 'neutral',
  unknown: '',
};

// Shown under every verdict, in the same words as the site, so a short popup never overstates a result.
export const VERDICT_CAVEAT = {
  verified_capture: 'This confirms where the file came from (its signed origin), not that the scene is real: a camera can photograph a screen or a staged scene.',
  verified_ai: 'Trusted credentials declare generative AI. They do not say which parts are synthetic.',
  ai: 'Markers can be faked and detectors make mistakes. Check where the file came from.',
  edited: 'Editing software was used. Edits can be harmless, and this does not mean the original was real.',
  camera: 'Camera metadata can be copied, edited or faked. It does NOT mean the file is real.',
  unknown: 'Finding nothing is not evidence that the file is real. Many fakes have their metadata removed.',
};

const TAGS = {
  ai: 'AI',
  edit: 'EDITED',
  camera: 'CAMERA',
  c2pa: 'C2PA',
  model: 'MODEL',
  info: 'INFO',
};

const DECISION_READING = {
  likely: 'Likely AI-generated',
  possible: 'Possible signs of AI',
  none: 'No strong AI signal',
};

function str(v) {
  return typeof v === 'string' ? v : '';
}

function pct(p) {
  return Math.max(1, Math.min(99, Math.round(Number(p) * 100)));
}

function stripNoProof(text) {
  return text.replace(/\s*No tool can prove a file is real\.\s*$/, '').trim();
}

function c2paRows(c) {
  let signature = 'Does NOT validate: the file may have been changed after signing';
  if (c.valid && c.trusted) signature = 'Valid, signer is on the trust list';
  else if (c.valid) signature = 'Intact, but the signer is not on the trust list';
  const verified = Boolean(c.valid && c.trusted);
  const claim = v => (v && !verified ? `Claimed (not verified): ${v}` : v);
  let capture = 'Not declared';
  if (c.capture && verified) capture = 'Yes, declared as a camera capture (origin, not proof the scene is real)';
  else if (c.claimed_capture || c.capture) capture = 'Claimed (not verified): declared as a camera capture';
  let ai = 'Not declared';
  if (c.ai_generated) ai = claim(c.ingredient_ai ? "Yes, declared by an ingredient's own credentials" : 'Yes, declared in the credentials');
  return [
    ['Signature', signature],
    ['AI-generated', ai],
    ['Capture evidence', capture],
    ['Made with', claim(str(c.generator))],
    ['Signed by', claim(str(c.signer) || str(c.issuer))],
  ].filter(([, v]) => v);
}

function modelRows(m) {
  const models = Array.isArray(m.models) && m.models.length ? m.models : [{ name: m.name, ai_probability: m.ai_probability }];
  let reading = DECISION_READING[m.decision] || '';
  if (m.low_colour) reading = 'Not reliable for black-and-white or low-colour images (scores shown for reference only)';
  else if (m.decision === 'none' && (m.frames_likely || m.frames_possible)) reading = 'No consistent AI signal across the clip';
  return [
    ['Combined reading', reading],
    ...models
      .filter(s => s && typeof s.ai_probability === 'number')
      .map(s => [str(s.name) || 'Model', `${pct(s.ai_probability)}% chance of AI`]),
  ].filter(([, v]) => v);
}

/**
 * Turn a /api/scan response into what the result window shows. Every value is plain text.
 * Returns null when the response does not look like a scan report.
 */
export function describeReport(r) {
  if (!r || typeof r !== 'object' || typeof r.verdict_title !== 'string' || typeof r.verdict !== 'string') return null;
  const verdict = Object.hasOwn(VERDICT_CLASS, r.verdict) ? r.verdict : 'unknown';
  const sections = [];
  if (r.c2pa && typeof r.c2pa === 'object') {
    sections.push({
      title: 'Content credentials (C2PA)',
      rows: c2paRows(r.c2pa),
      note: 'Check the full history at contentcredentials.org/verify.',
    });
  }
  if (r.model && typeof r.model === 'object') {
    const multi = Array.isArray(r.model.models) && r.model.models.length > 1;
    sections.push({
      title: 'AI detector models',
      rows: modelRows(r.model),
      note: (multi ? '"Likely AI-generated" needs both models to agree. ' : '')
        + 'Estimates from machine-learning models. They can be wrong both ways, and low scores do not mean the file is real.',
    });
  }
  const rawDetail = str(r.verdict_detail);
  // The site ends every non-verified detail with NO_PROOF after its own caveat; repeating a second caveat
  // there would only add noise. Verified verdicts (and details without it) always get the caveat.
  const siteWorded = rawDetail.trimEnd().endsWith(NO_PROOF) && !verdict.startsWith('verified_') && verdict === r.verdict;
  return {
    cls: VERDICT_CLASS[verdict],
    title: r.verdict_title,
    detail: stripNoProof(rawDetail),
    caveat: siteWorded ? '' : VERDICT_CAVEAT[verdict],
    noProof: NO_PROOF,
    signals: (Array.isArray(r.signals) ? r.signals : [])
      .filter(s => s && typeof s.text === 'string')
      .map(s => {
        const tag = Object.hasOwn(TAGS, s.kind) ? s.kind : 'info';
        return { tag, label: TAGS[tag], text: s.text };
      }),
    sections,
  };
}

const HTTP_ERRORS = {
  400: ['Could not scan', 'The server could not read the image.'],
  408: ['Upload timed out', 'The upload took too long. Try again on a faster connection.'],
  413: ['File too large', 'This image is over the server\'s upload limit.'],
  415: ['Unsupported image', 'The server can only scan JPG, PNG, WEBP and GIF images (and some videos).'],
  429: ['Slow down', 'Too many scans from your connection. Please wait a minute and try again.'],
  503: ['Scanner busy', 'The scanner is handling other files right now. Please try again in a moment.'],
  504: ['Scan timed out', 'The scan took too long. Please try again.'],
};

const VIDEO_HTTP_ERRORS = {
  404: ['Server does not support video checks', 'This BanTheFake server is too old to check videos. Ask its operator to update it.'],
  405: ['Server does not support video checks', 'This BanTheFake server is too old to check videos. Ask its operator to update it.'],
  413: ['Frames too large', 'The captured frames are over the server\'s upload limit.'],
  415: ['Could not check this video', 'The server could not read the captured frames.'],
};

const FETCH_ERRORS = {
  too_large: ['Image too large', 'This image is over 50 MB, so it was not sent.'],
  empty: ['Empty image', 'The image has no data, so it was not sent.'],
  blocked: [
    'Could not read this image',
    'The page\'s security rules do not let extensions read this image (it is served from another site without permission). Nothing was sent. Save the image and upload it on the BanTheFake site instead.',
  ],
  unsupported_url: ['Could not read this image', 'This kind of image address cannot be read. Nothing was sent. Save the image and upload it on the BanTheFake site instead.'],
  not_configured: ['Set up your server first', 'Choose which BanTheFake server to use in the extension options. Nothing was sent.'],
  network: ['Could not reach the scanner', 'Check your connection and the server address in the options, then try again.'],
  protected: ['This video can\'t be captured', 'This video can\'t be captured (protected or cross-origin) — nothing was sent.'],
  no_video: ['No video found', 'No visible video was found where you clicked. Start the video, then right-click it again. Nothing was sent.'],
  not_ready: ['Video not loaded yet', 'The video has no picture yet. Let it play for a moment, then try again. Nothing was sent.'],
  frames_too_large: ['Frames too large', 'The captured frames are over the 12 MB limit, so they were not sent.'],
  bad_response: ['Unexpected response', 'The server did not return a BanTheFake scan report. Check the server address in the options.'],
};

/** Title and message for a failure: `code` is an HTTP status (number) or a FETCH_ERRORS key. */
export function describeError(code, serverMessage, kind = 'image') {
  if (typeof code === 'number') {
    const table = kind === 'video' && Object.hasOwn(VIDEO_HTTP_ERRORS, code) ? VIDEO_HTTP_ERRORS : HTTP_ERRORS;
    const [title, detail] = table[code] || ['Scan failed', 'Something went wrong. Please try again.'];
    const msg = str(serverMessage);
    // A 415 message from the server names the accepted types precisely; short codes like "busy" add nothing.
    return { title, detail: code === 415 && msg.includes(' ') ? msg : detail };
  }
  if (typeof code === 'string' && code.startsWith('http_')) {
    return { title: 'Could not read this image', detail: `The image's website answered with an error (${code.slice(5)}). Nothing was sent.` };
  }
  const [title, detail] = FETCH_ERRORS[code] || FETCH_ERRORS.network;
  return { title, detail };
}

// ---------- Video checks ----------

/** Total bytes of captured frames ({ size }), and whether they fit the extension's payload cap. */
export function framesPayload(frames, cap = MAX_VIDEO_PAYLOAD_BYTES) {
  const bytes = (Array.isArray(frames) ? frames : []).reduce((n, f) => n + (Number(f?.size) || 0), 0);
  return { bytes, ok: bytes > 0 && bytes <= cap && frames.length <= MAX_VIDEO_FRAMES };
}

/** True for youtube.com, tiktok.com and their subdomains (platforms with their own "AI-generated" labels). */
export function isLabelPlatform(host) {
  const h = String(host || '').toLowerCase().replace(/\.$/, '');
  return LABEL_PLATFORMS.some(p => h === p || h.endsWith(`.${p}`));
}

function seconds(s) {
  const n = Math.max(1, Math.round(Number(s) || 0));
  return `~${n} second${n === 1 ? '' : 's'}`;
}

/**
 * What the result window says about a video check, besides the server's report. Never claims a video is real.
 * `capture`: { frames (count), spanS, paused, playbackStopped }, `host`: the page's hostname (shown only here;
 * it is never sent to the server).
 */
export function describeVideoCheck(capture, host) {
  const n = Math.max(0, Number(capture?.frames) || 0);
  let summary;
  if (capture?.paused) summary = 'Paused — 1 frame checked.';
  else if (n <= 1) summary = '1 frame checked (the video stopped or did not advance while capturing).';
  else summary = `${n} frames checked over ${seconds(capture.spanS)} of playback.`;
  if (!capture?.paused && capture?.playbackStopped && n > 1) summary += ' Playback stopped before all frames were captured.';
  const notes = [
    'Frames were copied from the video playing in your browser. Screen capture and re-compression make the detector less reliable, and audio is not analysed.',
    'AI-video detection has not been benchmarked yet: treat any result as a hint, not a finding.',
  ];
  if (isLabelPlatform(host)) notes.push("Platform labels (like 'AI-generated' tags) aren't read by this tool.");
  const rows = [['Checked', summary]];
  if (host) rows.push(['Video on', `${host} (the page address was not sent)`]);
  return { summary, rows, notes };
}
