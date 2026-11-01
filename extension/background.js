// Background worker: context menu -> read the clicked image -> POST it to the configured server -> result window.
// Images are only read and sent when the user picks "Check with BanTheFake"; nothing else ever leaves the browser.
import {
  DEFAULT_SERVER, MAX_IMAGE_BYTES, base64ToBytes, extensionFetchAllowed, filenameFromUrl, isConfigured,
  pageFetchImage, scanEndpoint, validateServerUrl,
} from './lib.js';

const api = globalThis.browser ?? globalThis.chrome;
const MENU_ID = 'btf-check-image';
const KEEP_JOBS = 5;

api.runtime.onInstalled.addListener(async details => {
  api.contextMenus.create({ id: MENU_ID, title: 'Check with BanTheFake', contexts: ['image'] });
  if (details.reason === 'install') {
    const { server } = await api.storage.local.get('server');
    if (!isConfigured(server)) api.runtime.openOptionsPage();
  }
});

api.action?.onClicked.addListener(() => api.runtime.openOptionsPage());

function onMenuClick(info, tab) {
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
