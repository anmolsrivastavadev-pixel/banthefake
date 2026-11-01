// Video checks: frame capture helpers (capture.js), payload cap and result wording (lib.js).
// Imported by lib.test.js, so `node --test extension/` runs it too.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  MAX_VIDEO_FRAMES, MAX_VIDEO_PAYLOAD_BYTES, NO_PROOF, VIDEO_PAGE_PATTERNS, describeError, describeVideoCheck,
  framesEndpoint, framesPayload, isLabelPlatform,
} from '../lib.js';
import '../capture.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const C = globalThis.BTFCapture;

// ---------- Pure helpers ----------

test('frames are downscaled to a 1280 px long side, never upscaled', () => {
  assert.deepEqual(C.fitSize(1920, 1080), { width: 1280, height: 720 });
  assert.deepEqual(C.fitSize(1080, 1920), { width: 720, height: 1280 });
  assert.deepEqual(C.fitSize(3840, 2160), { width: 1280, height: 720 });
  assert.deepEqual(C.fitSize(640, 360), { width: 640, height: 360 });
  assert.deepEqual(C.fitSize(4000, 1), { width: 1280, height: 1 });
  assert.deepEqual(C.fitSize(0, 720), { width: 0, height: 0 });
});

function rgba(fn, n = 64 * 36) {
  const out = new Uint8ClampedArray(n * 4);
  for (let i = 0; i < n; i++) {
    const [r, g, b] = fn(i);
    out.set([r, g, b, 255], i * 4);
  }
  return out;
}

test('black and near-uniform frames (DRM) look blank; real pictures do not', () => {
  assert.equal(C.looksBlank(rgba(() => [0, 0, 0])), true);
  assert.equal(C.looksBlank(rgba(() => [40, 40, 40])), true);
  assert.equal(C.looksBlank(rgba(i => [i % 3, 0, 1])), true); // encoder noise on black
  assert.equal(C.looksBlank(rgba(i => [(i * 7) % 256, (i * 3) % 256, i % 256])), false);
  assert.equal(C.looksBlank(rgba(i => (i < 1000 ? [0, 0, 0] : [255, 255, 255]))), false);
  assert.equal(C.looksBlank(new Uint8ClampedArray(0)), true);
});

test('a tainted canvas is recognised from the SecurityError', () => {
  assert.equal(C.isTaintError(new DOMException('The canvas has been tainted by cross-origin data.', 'SecurityError')), true);
  assert.equal(C.isTaintError({ name: 'Error', message: 'Failed to execute getImageData: tainted' }), true);
  assert.equal(C.isTaintError(new Error('encode failed')), false);
  assert.equal(C.isTaintError(null), false);
});

test('the clicked video wins, then the largest visible playing one, then the largest visible one', () => {
  const v = (area, playing, src = '', visible = true) => ({ area, playing, src, visible, ready: true });
  assert.equal(C.chooseVideo([v(100, true, 'a'), v(900, true, 'b')], 'a'), 0);
  assert.equal(C.chooseVideo([v(100, true), v(900, false), v(400, true)]), 2);
  assert.equal(C.chooseVideo([v(100, false), v(900, false)]), 1);
  assert.equal(C.chooseVideo([v(5000, true, '', false), v(100, false)]), 1); // hidden players are ignored
  assert.equal(C.chooseVideo([v(0, true)]), -1);
  assert.equal(C.chooseVideo([]), -1);
});

test('frame count: one when paused, up to 8 while playing, never past the end', () => {
  assert.equal(C.planFrameCount({ paused: true, duration: 100, currentTime: 1 }), 1);
  assert.equal(C.planFrameCount({ paused: false, ended: true, duration: 100, currentTime: 100 }), 1);
  assert.equal(C.planFrameCount({ paused: false, duration: 100, currentTime: 10 }), 8);
  assert.equal(C.planFrameCount({ paused: false, duration: 13.5, currentTime: 10 }), 4);
  assert.equal(C.planFrameCount({ paused: false, duration: 10.2, currentTime: 10 }), 1);
  assert.equal(C.planFrameCount({ paused: false, duration: Infinity, currentTime: 50 }), 8); // live stream
  assert.equal(C.planFrameCount({ paused: false, duration: NaN, currentTime: 0 }), 8);
});

test('payload budget counts frames and stops at the cap', () => {
  const budget = { used: 0, maxTotal: 10, maxFrame: 4 };
  assert.equal(C.admitFrame(budget, 4), 'ok');
  assert.equal(C.admitFrame(budget, 5), 'frame_too_large');
  assert.equal(C.admitFrame(budget, 4), 'ok');
  assert.equal(C.admitFrame(budget, 3), 'full');
  assert.equal(budget.used, 8);
});

test('the sampler grabs at once, then once per interval, and stops when playback stops', async () => {
  const sleeps = [];
  const sleep = async ms => sleeps.push(ms);
  const all = await C.sampleFrames({ count: 8, intervalMs: 1000, grab: async i => ({ frame: `f${i}` }), stillPlaying: () => true, sleep });
  assert.deepEqual(all.frames, ['f0', 'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7']);
  assert.equal(sleeps.length, 7);
  assert.equal(all.playbackStopped, false);

  // Frames are due at start + i * interval: time spent grabbing/encoding is taken off the next wait.
  let clock = 0;
  const waits = [];
  await C.sampleFrames({
    count: 4, intervalMs: 1000, stillPlaying: () => true, now: () => clock,
    grab: async () => { clock += 250; return { frame: 'x' }; },
    sleep: async ms => { waits.push(ms); clock += ms; },
  });
  assert.deepEqual(waits, [750, 750, 750]);
  assert.equal(clock, 3250);

  let ticks = 0;
  const stopped = await C.sampleFrames({
    count: 8, intervalMs: 1000, grab: async i => ({ frame: `f${i}` }), stillPlaying: () => ++ticks < 3, sleep: async () => {},
  });
  assert.deepEqual(stopped.frames, ['f0', 'f1', 'f2']);
  assert.equal(stopped.playbackStopped, true);

  const skipped = await C.sampleFrames({
    count: 4, intervalMs: 1, grab: async i => (i % 2 ? { skip: true } : { frame: `f${i}` }), stillPlaying: () => true, sleep: async () => {},
  });
  assert.deepEqual(skipped.frames, ['f0', 'f2']);

  const aborted = await C.sampleFrames({
    count: 8, intervalMs: 1, grab: async i => (i === 2 ? { stop: 'protected' } : { frame: i }), stillPlaying: () => true, sleep: async () => {},
  });
  assert.equal(aborted.stop, 'protected');
});

// ---------- capture() against a fake page ----------

function fakePage({
  paused = false, duration = 120, mediaKeys = null, taint = false, blank = false, blobSize = 300_000,
  readyState = 4, videos = 1, width = 1920, height = 1080, host = 'www.youtube.com',
} = {}) {
  const log = { draws: [], sleeps: [], playbackCalls: [] };
  let t = 10;
  const makeVideo = i => ({
    paused, ended: false, readyState, videoWidth: width, videoHeight: height, duration, mediaKeys,
    get currentTime() { return t; }, // getter only: assigning (seeking) would throw in strict mode
    currentSrc: `blob:https://${host}/v${i}`,
    getBoundingClientRect: () => (i === 0 ? { left: 0, top: 0, right: 1280, bottom: 720 } : { left: 0, top: 0, right: 100, bottom: 50 }),
    play() { log.playbackCalls.push('play'); },
    pause() { log.playbackCalls.push('pause'); },
  });
  const list = Array.from({ length: videos }, (_, i) => makeVideo(i));
  const canvas = () => {
    const c = {
      width: 0, height: 0,
      getContext: () => ({
        drawImage: (src, x, y, w, h) => log.draws.push([w, h]),
        getImageData: (x, y, w, h) => {
          if (taint) throw new DOMException('The canvas has been tainted by cross-origin data.', 'SecurityError');
          return { data: rgba(i => (blank ? [0, 0, 0] : [(i * 7) % 256, (i * 3) % 256, i % 256]), w * h) };
        },
      }),
      toBlob: (cb, type, q) => cb(new Blob([new Uint8Array(blobSize)], { type })),
    };
    return c;
  };
  const document = { querySelectorAll: sel => (sel === 'video' ? list : []), createElement: () => canvas() };
  const window = { innerWidth: 1280, innerHeight: 800, location: { hostname: host }, getComputedStyle: () => ({ visibility: 'visible', display: 'block', opacity: '1' }) };
  let clock = 0;
  const env = {
    document, window,
    sleep: async ms => { log.sleeps.push(ms); t += ms / 1000; clock += ms; },
    now: () => clock,
  };
  return { env, log, list };
}

test('capture: a playing video gives 8 JPEG frames over ~7 s without seeking or touching playback', async () => {
  const { env, log } = fakePage();
  const r = await C.capture({}, env);
  assert.equal(r.ok, true);
  assert.equal(r.frames.length, 8);
  assert.equal(r.paused, false);
  assert.equal(r.spanS, 7);
  assert.equal(r.host, 'www.youtube.com');
  assert.deepEqual([r.width, r.height], [1280, 720]);
  assert.ok(r.frames.every(f => f.type === 'image/jpeg' && f.size === 300_000 && typeof f.b64 === 'string'));
  assert.deepEqual(log.sleeps, Array(7).fill(1000));
  assert.deepEqual(log.playbackCalls, []);
  assert.deepEqual(log.draws.filter(([w]) => w === 1280).length, 8);
});

test('capture: a paused video gives exactly the current frame, at once', async () => {
  const { env, log } = fakePage({ paused: true });
  const r = await C.capture({}, env);
  assert.equal(r.ok, true);
  assert.equal(r.frames.length, 1);
  assert.equal(r.paused, true);
  assert.deepEqual(log.sleeps, []);
});

test('capture: a cross-origin (tainted) video stops with nothing returned', async () => {
  const { env } = fakePage({ taint: true });
  assert.deepEqual(await C.capture({}, env), { ok: false, error: 'protected', host: 'www.youtube.com' });
});

test('capture: black frames from protected (EME) video stop at once; blank frames otherwise return nothing', async () => {
  const drm = fakePage({ blank: true, mediaKeys: {} });
  assert.equal((await C.capture({}, drm.env)).error, 'protected');
  assert.deepEqual(drm.log.sleeps, []);
  const blank = fakePage({ blank: true });
  const r = await C.capture({}, blank.env);
  assert.equal(r.ok, false);
  assert.equal(r.error, 'protected');
});

test('capture: the payload cap limits how many frames are kept', async () => {
  const { env } = fakePage({ blobSize: 3.5 * 1024 * 1024 });
  const r = await C.capture({ maxTotalBytes: 12 * 1024 * 1024 }, env);
  assert.equal(r.ok, true);
  assert.equal(r.frames.length, 3);
  assert.ok(r.frames.reduce((n, f) => n + f.size, 0) <= 12 * 1024 * 1024);
});

test('capture: no video, or no picture yet', async () => {
  assert.equal((await C.capture({}, fakePage({ videos: 0 }).env)).error, 'no_video');
  assert.equal((await C.capture({}, fakePage({ readyState: 1 }).env)).error, 'not_ready');
});

test('capture: the largest on-screen video is chosen', async () => {
  const { env, log } = fakePage({ videos: 2 });
  const r = await C.capture({}, env);
  assert.equal(r.ok, true);
  assert.deepEqual(log.draws[0], [1280, 720]);
});

// ---------- Payload and wording ----------

test('the extension sends at most 8 frames and 12 MB to /api/scan-frames', () => {
  assert.equal(MAX_VIDEO_FRAMES, 8);
  assert.equal(MAX_VIDEO_PAYLOAD_BYTES, 12 * 1024 * 1024);
  assert.equal(framesEndpoint('https://btf.example.org'), 'https://btf.example.org/api/scan-frames');
  assert.deepEqual(framesPayload([{ size: 1000 }, { size: 2000 }]), { bytes: 3000, ok: true });
  assert.equal(framesPayload([{ size: MAX_VIDEO_PAYLOAD_BYTES }, { size: 1 }]).ok, false);
  assert.equal(framesPayload(Array(9).fill({ size: 1 })).ok, false);
  assert.equal(framesPayload([]).ok, false);
});

test('video wording: frame count and span, paused case, host only, never "real"', () => {
  const playing = describeVideoCheck({ frames: 8, spanS: 7.2, paused: false }, 'www.youtube.com');
  assert.equal(playing.summary, '8 frames checked over ~7 seconds of playback.');
  assert.deepEqual(playing.rows[1], ['Video on', 'www.youtube.com (the page address was not sent)']);
  assert.ok(playing.notes.some(n => /audio is not analysed/.test(n)));
  assert.ok(playing.notes.some(n => /not been benchmarked/.test(n)));
  assert.ok(playing.notes.includes("Platform labels (like 'AI-generated' tags) aren't read by this tool."));

  const paused = describeVideoCheck({ frames: 1, spanS: 0, paused: true }, 'www.tiktok.com');
  assert.equal(paused.summary, 'Paused — 1 frame checked.');
  assert.ok(paused.notes.some(n => /Platform labels/.test(n)));

  const elsewhere = describeVideoCheck({ frames: 3, spanS: 2, paused: false, playbackStopped: true }, 'example.org');
  assert.match(elsewhere.summary, /^3 frames checked over ~2 seconds of playback\. Playback stopped/);
  assert.equal(elsewhere.notes.some(n => /Platform labels/.test(n)), false);

  for (const d of [playing, paused, elsewhere]) {
    const text = [d.summary, ...d.notes, ...d.rows.flat()].join(' ');
    assert.doesNotMatch(text, /\breal\b|authentic|genuine/i);
    assert.doesNotMatch(text, /https?:\/\//); // the host only, never the page URL
  }
  assert.equal(NO_PROOF, 'No tool can prove a file is real.'); // still shown under every result
});

test('label platforms are youtube.com and tiktok.com (and subdomains) only', () => {
  for (const h of ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'www.tiktok.com', 'TIKTOK.COM.']) assert.equal(isLabelPlatform(h), true, h);
  for (const h of ['notyoutube.com', 'youtube.com.evil.org', 'vimeo.com', '', undefined]) assert.equal(isLabelPlatform(h), false, String(h));
});

test('video errors say clearly that nothing was sent', () => {
  assert.equal(describeError('protected', '', 'video').detail,
    "This video can't be captured (protected or cross-origin) — nothing was sent.");
  assert.match(describeError('no_video', '', 'video').detail, /Nothing was sent/);
  assert.match(describeError('not_ready', '', 'video').detail, /Nothing was sent/);
  assert.match(describeError('frames_too_large', '', 'video').detail, /not sent/);
  assert.match(describeError(404, 'Not Found', 'video').title, /does not support video/);
  assert.match(describeError(413, '', 'video').title, /Frames too large/);
  assert.equal(describeError(413).title, 'File too large'); // images unchanged
});

// ---------- Static guarantees ----------

test('capture.js is a classic script that only reads pixels: no fetch, no seeking, no playback changes', () => {
  const src = readFileSync(join(ROOT, 'capture.js'), 'utf8');
  assert.doesNotMatch(src, /^\s*(import|export)\s/m);
  assert.doesNotMatch(src, /\bfetch\(|XMLHttpRequest|WebSocket|sendBeacon|\.src\s*=(?!=)|currentTime\s*=(?!=)|playbackRate\s*=|\.play\(|\.pause\(|\.load\(|fastSeek/);
});

test('the video job sends only the frames: no page URL, title or other fields', () => {
  const src = readFileSync(join(ROOT, 'background.js'), 'utf8');
  const appends = [...src.matchAll(/body\.append\('([a-z]+)'/g)].map(m => m[1]);
  assert.deepEqual([...new Set(appends)].sort(), ['file', 'frames']);
  assert.doesNotMatch(src, /append\([^)]*(pageUrl|frameUrl|location|title)/);
  // The page-level menu entry is limited to YouTube/TikTok; the video entry uses the "video" context.
  assert.deepEqual(VIDEO_PAGE_PATTERNS, ['https://*.youtube.com/*', 'https://*.tiktok.com/*']);
  assert.match(src, /contexts: \['video'\]/);
  assert.match(src, /contexts: \['page'\],\s*documentUrlPatterns: VIDEO_PAGE_PATTERNS/);
});
