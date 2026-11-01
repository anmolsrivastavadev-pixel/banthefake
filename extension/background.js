// Background worker: context menu -> read the clicked image (or capture frames of the playing video) -> POST it to
// the configured server -> result window. Media is only read and sent when the user picks a BanTheFake menu entry;
// nothing else ever leaves the browser. The page address is never sent.
import {
  DEFAULT_SERVER, MAX_IMAGE_BYTES, MAX_VIDEO_FRAMES, MAX_VIDEO_PAYLOAD_BYTES, VIDEO_PAGE_PATTERNS, base64ToBytes,
  extensionFetchAllowed, filenameFromUrl, framesEndpoint, framesPayload, isConfigured, pageFetchImage, scanEndpoint,
  validateServerUrl,
} from './lib.js';

const api = globalThis.browser ?? globalThis.chrome;
const MENU_ID = 'btf-check-image';
const MENU_VIDEO = 'btf-check-video';
const MENU_PAGE_VIDEO = 'btf-check-page-video';
const KEEP_JOBS = 5;

api.runtime.onInstalled.addListener(async details => {
  api.contextMenus.create({ id: MENU_ID, title: 'Check with BanTheFake', contexts: ['image'] });
  api.contextMenus.create({ id: MENU_VIDEO, title: 'Check this video with BanTheFake', contexts: ['video'] });
  // On YouTube and TikTok the player's overlays catch the right-click, so the browser offers the "page" menu,
  // not the "video" one. This entry is limited to those sites; elsewhere the page menu is unchanged.
  api.contextMenus.create({
    id: MENU_PAGE_VIDEO, title: 'Check the video on this page with BanTheFake', contexts: ['page'],
    documentUrlPatterns: VIDEO_PAGE_PATTERNS,
  });
  if (details.reason === 'install') {
    const { server } = await api.storage.local.get('server');
    if (!isConfigured(server)) api.runtime.openOptionsPage();
  }
});

api.action?.onClicked.addListener(() => api.runtime.openOptionsPage());

function onMenuClick(info, tab) {
  if (info.menuItemId === MENU_VIDEO || info.menuItemId === MENU_PAGE_VIDEO) {
    startVideoJob(info, tab);
    return;
  }
  if (info.menuItemId !== MENU_ID || !info.srcUrl) return;
  // Open the window first, synchronously in the click handler, then fill it in as the scan progresses.
  const id = `job-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ready = setJob(id, { status: 'loading', imageHost: hostOf(info.srcUrl) });
  api.windows.create({ url: api.runtime.getURL(`result.html#${id}`), type: 'popup', width: 440, height: 640 });
  ready.then(() => runScan(id, info, tab)).catch(() => setJob(id, { status: 'error', code: 'network' }));
}

api.contextMenus.onClicked.addListener(onMenuClick);
// Browser automation cannot open native context menus: the smoke test calls this from the worker's own
// console. Only code running inside this extension can reach it.
globalThis.btfMenuClick = onMenuClick;

function hostOf(src) {
  try {
    const u = new URL(src);
    return u.protocol === 'data:' || u.protocol === 'blob:' ? 'this page' : u.hostname;
  } catch {
    return '';
  }
}

async function setJob(id, value) {
  const { jobs = [] } = await api.storage.session.get('jobs');
  const keep = [id, ...jobs.filter(j => j !== id)].slice(0, KEEP_JOBS);
  const stale = jobs.filter(j => !keep.includes(j));
  if (stale.length) await api.storage.session.remove(stale);
  await api.storage.session.set({ jobs: keep, [id]: value });
}

async function readImage(info, tab) {
  // 1. In the page, with the page's origin: same-origin and CORS rules apply exactly as for the page itself.
  if (tab?.id !== undefined && tab.id >= 0) {
    try {
      const [res] = await api.scripting.executeScript({
        target: { tabId: tab.id, frameIds: [info.frameId ?? 0] },
        func: pageFetchImage,
        args: [info.srcUrl, MAX_IMAGE_BYTES],
      });
      const out = res?.result;
      if (out?.ok) return { ok: true, bytes: base64ToBytes(out.b64), type: out.type };
      if (out && out.error !== 'blocked') return out;
    } catch {
      // Pages the extension may not script (browser pages, web stores): fall through.
    }
  }
  // 2. From the extension, without cookies, for https: and data: images. Still subject to CORS: the
  //    extension has no host permission for the image's site, so the site must allow cross-origin reads.
  if (!extensionFetchAllowed(info.srcUrl)) return { ok: false, error: 'blocked' };
  try {
    const res = await fetch(info.srcUrl, { credentials: 'omit', cache: 'force-cache', referrerPolicy: 'no-referrer' });
    if (!res.ok) return { ok: false, error: `http_${res.status}` };
    const blob = await res.blob();
    if (blob.size > MAX_IMAGE_BYTES) return { ok: false, error: 'too_large' };
    if (blob.size === 0) return { ok: false, error: 'empty' };
    return { ok: true, bytes: blob, type: blob.type };
  } catch {
    return { ok: false, error: 'blocked' };
  }
}

async function runScan(id, info, tab) {
  const { server = DEFAULT_SERVER } = await api.storage.local.get('server');
  const v = validateServerUrl(server);
  if (!v.ok || !isConfigured(server)) {
    await setJob(id, { status: 'error', code: 'not_configured' });
    return;
  }
  const base = v.base;
  await setJob(id, { status: 'loading', imageHost: hostOf(info.srcUrl), server: base });

  const image = await readImage(info, tab);
  if (!image.ok) {
    await setJob(id, { status: 'error', code: image.error, server: base });
    return;
  }
  const body = new FormData();
  const blob = image.bytes instanceof Blob ? image.bytes : new Blob([image.bytes], { type: image.type || 'application/octet-stream' });
  body.append('file', blob, filenameFromUrl(info.srcUrl));

  let res, data;
  try {
    // No cookies, no referrer, and no redirects: a redirect could re-send the image to another server.
    res = await fetch(scanEndpoint(base), {
      method: 'POST', body, credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', cache: 'no-store',
    });
    data = await res.json().catch(() => null);
  } catch {
    await setJob(id, { status: 'error', code: 'network', server: base });
    return;
  }
  if (!res.ok) {
    await setJob(id, { status: 'error', code: res.status, message: data?.error ?? '', server: base });
    return;
  }
  await setJob(id, { status: 'done', report: data, server: base, imageHost: hostOf(info.srcUrl) });
}

// ---------- Video: capture frames of the video the user is playing ----------

function startVideoJob(info, tab) {
  const id = `job-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const pageHost = hostOf(info.frameUrl || info.pageUrl || '');
  const ready = setJob(id, { kind: 'video', status: 'capturing', pageHost });
  api.windows.create({ url: api.runtime.getURL(`result.html#${id}`), type: 'popup', width: 440, height: 680 });
  ready.then(() => runVideoScan(id, info, tab, pageHost))
    .catch(() => setJob(id, { kind: 'video', status: 'error', code: 'network', pageHost }));
}

async function captureFrames(info, tab) {
  if (tab?.id === undefined || tab.id < 0) return { ok: false, error: 'protected' };
  const target = { tabId: tab.id, frameIds: [info.frameId ?? 0] };
  try {
    // activeTab (granted by this click) lets us script the clicked frame once. capture.js only reads pixels from
    // the <video> already on the page; it never fetches anything and never seeks or pauses.
    await api.scripting.executeScript({ target, files: ['capture.js'] });
    const [res] = await api.scripting.executeScript({
      target,
      func: opts => globalThis.BTFCapture.capture(opts),
      args: [{ srcUrl: info.srcUrl || '', maxFrames: MAX_VIDEO_FRAMES, maxTotalBytes: MAX_VIDEO_PAYLOAD_BYTES }],
    });
    return res?.result ?? { ok: false, error: 'protected' };
  } catch {
    // A frame from another site, or a page extensions may not script: nothing can be captured.
    return { ok: false, error: 'protected' };
  }
}

async function runVideoScan(id, info, tab, pageHost) {
  const { server = DEFAULT_SERVER } = await api.storage.local.get('server');
  const v = validateServerUrl(server);
  if (!v.ok || !isConfigured(server)) {
    await setJob(id, { kind: 'video', status: 'error', code: 'not_configured', pageHost });
    return;
  }
  const base = v.base;
  await setJob(id, { kind: 'video', status: 'capturing', pageHost, server: base });

  const cap = await captureFrames(info, tab);
  const host = (cap && typeof cap.host === 'string' && cap.host) || pageHost;
  if (!cap?.ok) {
    await setJob(id, { kind: 'video', status: 'error', code: cap?.error || 'protected', server: base, pageHost: host });
    return;
  }
  const frames = Array.isArray(cap.frames) ? cap.frames.slice(0, MAX_VIDEO_FRAMES) : [];
  if (!framesPayload(frames).ok) {
    await setJob(id, { kind: 'video', status: 'error', code: frames.length ? 'frames_too_large' : 'protected', server: base, pageHost: host });
    return;
  }
  const capture = { frames: frames.length, spanS: Number(cap.spanS) || 0, paused: Boolean(cap.paused), playbackStopped: Boolean(cap.playbackStopped) };
  await setJob(id, { kind: 'video', status: 'loading', server: base, pageHost: host, capture });

  // Only the frames are sent: no page address, title, cookies or referrer.
  const body = new FormData();
  frames.forEach((f, i) => {
    body.append('frames', new Blob([base64ToBytes(f.b64)], { type: 'image/jpeg' }), `frame-${String(i).padStart(2, '0')}.jpg`);
  });
  let res, data;
  try {
    res = await fetch(framesEndpoint(base), {
      method: 'POST', body, credentials: 'omit', redirect: 'error', referrerPolicy: 'no-referrer', cache: 'no-store',
    });
    data = await res.json().catch(() => null);
  } catch {
    await setJob(id, { kind: 'video', status: 'error', code: 'network', server: base, pageHost: host });
    return;
  }
  if (!res.ok) {
    await setJob(id, { kind: 'video', status: 'error', code: res.status, message: data?.error ?? '', server: base, pageHost: host });
    return;
  }
  await setJob(id, { kind: 'video', status: 'done', report: data, server: base, pageHost: host, capture });
}
