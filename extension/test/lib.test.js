// Unit tests for the extension's pure logic: `node --test extension/` (Node 20+, no dependencies).
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  DEFAULT_SERVER, MAX_IMAGE_BYTES, NO_PROOF, VERDICT_CAVEAT, VERDICT_CLASS, base64ToBytes, describeError,
  describeReport, extensionFetchAllowed, filenameFromUrl, helpUrl, isConfigured, originPattern, pageFetchImage,
  scanEndpoint, validateServerUrl,
} from '../lib.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

// ---------- Server URL validation ----------

test('accepts https servers and normalizes them', () => {
  assert.deepEqual(validateServerUrl(' https://btf.example.org/ '), { ok: true, base: 'https://btf.example.org', origin: 'https://btf.example.org' });
  assert.equal(validateServerUrl('https://btf.example.org:8443').base, 'https://btf.example.org:8443');
  assert.equal(validateServerUrl('https://example.org/banthefake/').base, 'https://example.org/banthefake');
  assert.equal(validateServerUrl('HTTPS://BTF.Example.ORG').base, 'https://btf.example.org');
});

test('allows plain http only for localhost development', () => {
  assert.equal(validateServerUrl('http://localhost:8000').ok, true);
  assert.equal(validateServerUrl('http://127.0.0.1:8880/').base, 'http://127.0.0.1:8880');
  for (const bad of ['http://btf.example.org', 'http://192.168.1.10', 'http://localhost.evil.org', 'http://127.0.0.2', 'http://[::1]:8000']) {
    const v = validateServerUrl(bad);
    assert.equal(v.ok, false, bad);
  }
  assert.match(validateServerUrl('http://btf.example.org').error, /https/);
});

test('rejects other schemes, credentials, queries, fragments and junk', () => {
  for (const bad of [
    '', '   ', null, undefined, 'btf.example.org', 'ftp://btf.example.org', 'javascript:alert(1)', 'file:///etc/passwd',
    'data:text/html,hi', 'chrome-extension://abc', 'https://user:pw@btf.example.org', 'https://user@btf.example.org',
    'https://btf.example.org/?x=1', 'https://btf.example.org/#x', 'https://btf.example.org/?', 'https://bad host.org',
    'https://btf.example.org/"><script>', 'https://', 'wss://btf.example.org',
  ]) {
    const v = validateServerUrl(bad);
    assert.equal(v.ok, false, String(bad));
    assert.equal(typeof v.error, 'string');
  }
});

test('the default server is a placeholder that is never used', () => {
  assert.equal(validateServerUrl(DEFAULT_SERVER).ok, true);
  assert.equal(isConfigured(DEFAULT_SERVER), false);
  for (const placeholder of ['https://banthefake.example', 'https://x.example.com', 'https://a.test', 'https://b.invalid']) {
    assert.equal(isConfigured(placeholder), false, placeholder);
  }
  assert.equal(isConfigured(undefined), false);
  assert.equal(isConfigured('http://evil.org'), false);
  assert.equal(isConfigured('https://banthefake.org'), true);
  assert.equal(isConfigured('http://localhost:8880'), true);
});

test('host permission covers exactly the server host', () => {
  assert.equal(originPattern('https://btf.example.org:8443/sub'), 'https://btf.example.org/*');
  assert.equal(originPattern('http://localhost:8880'), 'http://localhost/*');
  assert.equal(originPattern('http://evil.org'), null);
  // Every pattern we can request is listed in optional_host_permissions.
  const manifest = JSON.parse(readFileSync(join(ROOT, 'manifest.json'), 'utf8'));
  const optional = manifest.optional_host_permissions;
  for (const s of ['https://a.org', 'http://localhost:1', 'http://127.0.0.1:2']) {
    const p = originPattern(s);
    assert.ok(optional.includes(p) || (p.startsWith('https://') && optional.includes('https://*/*')), p);
  }
});

test('endpoints are built from the normalized base', () => {
  assert.equal(scanEndpoint('https://a.org/btf'), 'https://a.org/btf/api/scan');
  assert.equal(helpUrl('https://a.org'), 'https://a.org/#report');
});

// ---------- Image fetching helpers ----------

test('filenames are short and safe', () => {
  assert.equal(filenameFromUrl('https://cdn.org/a/b/photo%20one.jpg?x=1'), 'photo_one.jpg');
  assert.equal(filenameFromUrl('data:image/png;base64,AAAA'), 'image');
  assert.equal(filenameFromUrl('https://cdn.org/'), 'image');
  assert.equal(filenameFromUrl('not a url'), 'image');
  assert.equal(filenameFromUrl(`https://cdn.org/${'a'.repeat(300)}.png`).length, 100);
  assert.equal(filenameFromUrl('https://cdn.org/..%2F..%2Fetc%2Fpasswd'), '.._.._etc_passwd');
});

test('the extension itself only fetches https and data images', () => {
  assert.equal(extensionFetchAllowed('https://cdn.org/a.jpg'), true);
  assert.equal(extensionFetchAllowed('data:image/png;base64,AA=='), true);
  for (const src of ['http://cdn.org/a.jpg', 'blob:https://a.org/123', 'file:///a.jpg', 'chrome://favicon/x', 'junk']) {
    assert.equal(extensionFetchAllowed(src), false, src);
  }
});

test('pageFetchImage reads bytes and enforces the size limit', async () => {
  const bytes = Uint8Array.from({ length: 70000 }, (_, i) => i % 256);
  const src = `data:image/png;base64,${Buffer.from(bytes).toString('base64')}`;
  const out = await pageFetchImage(src, MAX_IMAGE_BYTES);
  assert.equal(out.ok, true);
  assert.equal(out.size, 70000);
  assert.equal(out.type, 'image/png');
  assert.deepEqual(base64ToBytes(out.b64), bytes);
  assert.deepEqual(await pageFetchImage(src, 1000), { ok: false, error: 'too_large' });
  assert.deepEqual(await pageFetchImage('data:image/png;base64,', 1000), { ok: false, error: 'empty' });
  assert.deepEqual(await pageFetchImage('https://127.0.0.1:1/x.jpg', 1000), { ok: false, error: 'blocked' });
});

test('pageFetchImage is self-contained (it is serialized into the page)', () => {
  const source = pageFetchImage.toString();
  // Only globals the page has: no imports, helpers or module constants.
  for (const name of ['MAX_IMAGE_BYTES', 'base64ToBytes', 'api.', 'chrome.', 'browser.']) {
    assert.equal(source.includes(name), false, name);
  }
  // It must still work after a round trip through its source text.
  const clone = new Function(`return (${source})`)();
  assert.equal(typeof clone, 'function');
});

// ---------- Verdict rendering ----------

const report = (over = {}) => ({
  verdict: 'unknown',
  verdict_title: 'No conclusive signals found',
  verdict_detail: `That does NOT mean it is real. Many fakes have their metadata removed. ${NO_PROOF}`,
  signals: [],
  c2pa: null,
  model: null,
  file: { type: 'image', mime: 'image/jpeg', size_bytes: 100 },
  ...over,
});

test('every verdict has a style and an honest caveat; camera is never styled as verified', () => {
  for (const v of ['verified_capture', 'verified_ai', 'ai', 'edited', 'camera', 'unknown']) {
    assert.ok(v in VERDICT_CLASS, v);
    assert.ok(VERDICT_CAVEAT[v].length > 20, v);
    const view = describeReport(report({ verdict: v }));
    assert.equal(view.noProof, 'No tool can prove a file is real.');
  }
  assert.equal(VERDICT_CLASS.camera, 'neutral');
  assert.equal(VERDICT_CLASS.unknown, '');
  assert.match(VERDICT_CAVEAT.camera, /does NOT mean the file is real/);
  assert.match(VERDICT_CAVEAT.unknown, /not evidence that the file is real/);
});

test('verified_capture is described as origin, not scene', () => {
  const view = describeReport(report({
    verdict: 'verified_capture',
    verdict_title: 'Verified camera capture',
    verdict_detail: 'Trusted credentials signed by Google LLC declare a camera capture, and the file is unchanged since signing.',
    c2pa: { present: true, valid: true, trusted: true, capture: true, claimed_capture: true, signer: 'Google LLC', generator: 'Pixel Camera', ai_generated: false },
  }));
  assert.equal(view.cls, 'verified');
  assert.equal(view.title, 'Verified camera capture');
  assert.match(view.caveat, /where the file came from/);
  assert.match(view.caveat, /not that the scene is real/);
  const rows = Object.fromEntries(view.sections[0].rows);
  assert.equal(rows.Signature, 'Valid, signer is on the trust list');
  assert.match(rows['Capture evidence'], /origin, not proof the scene is real/);
  assert.equal(rows['Signed by'], 'Google LLC');
});

test('the caveat is added unless the site already worded the detail honestly', () => {
  // Site wording (ends with "No tool can prove...") is kept as is, with the standalone no-proof line.
  const camera = describeReport(report({ verdict: 'camera', verdict_detail: `It can be faked. ${NO_PROOF}` }));
  assert.equal(camera.caveat, '');
  assert.equal(camera.detail, 'It can be faked.');
  // Anything else gets the extension's own caveat for that verdict.
  const bare = describeReport(report({ verdict: 'camera', verdict_detail: 'Camera data found.' }));
  assert.equal(bare.caveat, VERDICT_CAVEAT.camera);
  const verified = describeReport(report({ verdict: 'verified_ai', verdict_detail: `Declared. ${NO_PROOF}` }));
  assert.equal(verified.caveat, VERDICT_CAVEAT.verified_ai);
});

test('untrusted credentials are shown as claims only', () => {
  const view = describeReport(report({
    c2pa: { present: true, valid: true, trusted: false, capture: false, claimed_capture: true, signer: 'Someone', generator: 'Tool', ai_generated: true },
  }));
  const rows = Object.fromEntries(view.sections[0].rows);
  assert.equal(rows.Signature, 'Intact, but the signer is not on the trust list');
  assert.equal(rows['Capture evidence'], 'Claimed (not verified): declared as a camera capture');
  assert.equal(rows['Signed by'], 'Claimed (not verified): Someone');
  assert.match(rows['AI-generated'], /^Claimed \(not verified\)/);
  const invalid = Object.fromEntries(describeReport(report({ c2pa: { present: true, valid: false } })).sections[0].rows);
  assert.match(invalid.Signature, /Does NOT validate/);
});

test('"likely AI" from the models keeps the server wording and both model scores', () => {
  const view = describeReport(report({
    verdict: 'ai',
    verdict_title: 'Likely AI-generated',
    verdict_detail: `No AI metadata, but both of our detector models agree it looks AI-made (H 99%, C 40%). Detectors make mistakes, so check where the file came from. ${NO_PROOF}`,
    signals: [{ kind: 'model', text: 'Both models agree.', weight: 0.8 }],
    model: { name: 'ensemble', decision: 'likely', ai_probability: 0.99, frames_checked: 1,
      models: [{ name: 'H', ai_probability: 0.995 }, { name: 'C', ai_probability: 0.4 }] },
  }));
  assert.equal(view.cls, 'warn');
  assert.equal(view.title, 'Likely AI-generated');
  // The trailing "No tool can prove..." is shown once, as its own line, not duplicated in the detail.
  assert.equal(view.detail.includes(NO_PROOF), false);
  assert.match(view.detail, /Detectors make mistakes/);
  assert.equal(view.noProof, NO_PROOF);
  const model = view.sections.find(s => s.title === 'AI detector models');
  assert.deepEqual(model.rows, [['Combined reading', 'Likely AI-generated'], ['H', '99% chance of AI'], ['C', '40% chance of AI']]);
  assert.match(model.note, /needs both models to agree/);
  assert.match(model.note, /low scores do not mean the file is real/);
  assert.deepEqual(view.signals, [{ tag: 'model', label: 'MODEL', text: 'Both models agree.' }]);
});

test('low-colour images never read as likely', () => {
  const view = describeReport(report({ model: { decision: 'likely', low_colour: true, ai_probability: 0.99, models: [{ name: 'C', ai_probability: 0.99 }] } }));
  assert.match(view.sections[0].rows[0][1], /Not reliable/);
});

test('signals are plain text and unknown kinds become info', () => {
  const view = describeReport(report({
    signals: [
      { kind: 'ai', text: '<img src=x onerror=alert(1)>', weight: 1 },
      { kind: '__proto__', text: 'weird', weight: 0 },
      { kind: 'camera', text: 42 },
      null,
    ],
  }));
  assert.deepEqual(view.signals, [
    { tag: 'ai', label: 'AI', text: '<img src=x onerror=alert(1)>' },
    { tag: 'info', label: 'INFO', text: 'weird' },
  ]);
});

test('unknown verdicts fall back to the most cautious wording', () => {
  const view = describeReport(report({ verdict: 'real', verdict_title: 'Real!' }));
  assert.equal(view.cls, '');
  assert.equal(view.caveat, VERDICT_CAVEAT.unknown);
});

test('non-reports are rejected', () => {
  for (const bad of [null, undefined, 'x', 1, [], {}, { verdict: 'ai' }, { verdict_title: 'x' }, { error: 'busy' }]) {
    assert.equal(describeReport(bad), null, JSON.stringify(bad));
  }
});

test('errors say whether anything was sent', () => {
  assert.equal(describeError(429).title, 'Slow down');
  assert.equal(describeError(503, 'busy').detail, 'The scanner is handling other files right now. Please try again in a moment.');
  assert.match(describeError(415, 'Unsupported file type. Upload a JPG, PNG.').detail, /^Unsupported file type/);
  assert.equal(describeError(500, 'scan_failed').title, 'Scan failed');
  assert.match(describeError('blocked').detail, /Nothing was sent/);
  assert.match(describeError('not_configured').detail, /Nothing was sent/);
  assert.match(describeError('too_large').detail, /not sent/);
  assert.match(describeError('http_404').detail, /\(404\)\. Nothing was sent/);
  assert.equal(describeError('whatever').title, 'Could not reach the scanner');
});

// ---------- Packaging guarantees ----------

test('manifest: MV3, no broad required host access, strict CSP', () => {
  const m = JSON.parse(readFileSync(join(ROOT, 'manifest.json'), 'utf8'));
  assert.equal(m.manifest_version, 3);
  assert.deepEqual([...m.permissions].sort(), ['activeTab', 'contextMenus', 'scripting', 'storage']);
  assert.equal(m.host_permissions, undefined);
  assert.equal(JSON.stringify(m).includes('<all_urls>'), false);
  assert.equal(m.content_scripts, undefined);
  const csp = m.content_security_policy.extension_pages;
  assert.match(csp, /script-src 'self';/);
  assert.match(csp, /object-src 'none'/);
  assert.doesNotMatch(csp, /unsafe-|wasm|http:\s|\*;|https:\/\/\*/);
});

test('extension pages load no remote code and use no inline scripts or handlers', () => {
  for (const f of readdirSync(ROOT).filter(n => /\.(html|js)$/.test(n))) {
    const src = readFileSync(join(ROOT, f), 'utf8');
    assert.doesNotMatch(src, /<script(?![^>]*\bsrc="[a-z]+\.js")/, `${f}: inline script`);
    assert.doesNotMatch(src, /\son[a-z]+="/, `${f}: inline handler`);
    assert.doesNotMatch(src, /src="https?:|import\s*\(|\beval\(|new Function|innerHTML|insertAdjacentHTML|document\.write/, `${f}: remote or dynamic code`);
  }
});

// Video-check tests live in their own file; `node --test extension/` only loads this one (package.json "main").
import './video.test.js';
