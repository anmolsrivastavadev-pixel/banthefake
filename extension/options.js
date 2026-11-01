import { DEFAULT_SERVER, healthEndpoint, isConfigured, originPattern, validateServerUrl } from './lib.js';

const api = globalThis.browser ?? globalThis.chrome;
const form = document.getElementById('form');
const input = document.getElementById('server');
const msg = document.getElementById('msg');
document.getElementById('extOrigin').textContent = new URL(api.runtime.getURL('')).origin;

function say(text, kind = '') {
  msg.textContent = text;
  msg.className = `msg ${kind}`;
}

function check(value) {
  const v = validateServerUrl(value);
  if (!v.ok) return v;
  if (!isConfigured(v.base)) return { ok: false, error: 'This is the placeholder address. Enter the address of a real BanTheFake server.' };
  return v;
}

api.storage.local.get('server').then(({ server }) => {
  input.value = server || DEFAULT_SERVER;
  if (!isConfigured(input.value)) say('Not set up yet: enter your BanTheFake server and save.');
});

form.addEventListener('submit', async e => {
  e.preventDefault();
  const v = check(input.value);
  if (!v.ok) {
    say(v.error, 'err');
    input.setAttribute('aria-invalid', 'true');
    return;
  }
  input.removeAttribute('aria-invalid');
  const pattern = originPattern(v.base);
  // Must be the first await in the click handler: the permission prompt needs the user gesture.
  let granted = false;
  try {
    granted = await api.permissions.request({ origins: [pattern] });
  } catch {
    granted = false;
  }
  const { server: previous } = await api.storage.local.get('server');
  const oldPattern = previous && originPattern(previous);
  if (oldPattern && oldPattern !== pattern) {
    // Never keep access to a server the user no longer uses.
    await api.permissions.remove({ origins: [oldPattern] }).catch(() => {});
  }
  await api.storage.local.set({ server: v.base });
  input.value = v.base;
  say(granted
    ? `Saved. Images you choose to check will be sent to ${v.base}.`
    : `Saved, but permission to connect to ${new URL(v.base).host} was not granted. Scanning will only work if that server allows this extension (BTF_CORS_ORIGINS).`,
  granted ? 'ok' : 'err');
});

document.getElementById('test').addEventListener('click', async () => {
  const v = check(input.value);
  if (!v.ok) {
    say(v.error, 'err');
    return;
  }
  say('Testing…');
  try {
    const res = await fetch(healthEndpoint(v.base), { credentials: 'omit', redirect: 'error', cache: 'no-store' });
    const data = await res.json().catch(() => null);
    if (res.ok && data && data.ok === true) say(`Connected. AI detector: ${String(data.model)}.`, 'ok');
    else say(`The server answered, but not like a BanTheFake server (HTTP ${res.status}).`, 'err');
  } catch {
    say('Could not connect. Check the address, save to grant access, or ask the operator to allow this extension.', 'err');
  }
});
