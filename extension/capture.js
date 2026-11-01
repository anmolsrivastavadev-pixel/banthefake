// Frame capture for "Check this video with BanTheFake". Injected on click only (chrome.scripting.executeScript
// `files`, in the extension's isolated world of the clicked frame, granted by activeTab), then called once.
// A classic script on purpose (no import/export), so it can be injected as a file; `node --test` imports it for its
// side effect and tests the pure helpers on globalThis.BTFCapture.
//
// What it does: finds the video the user is watching, and copies up to 8 frames of it to a canvas while it keeps
// playing (about one per second, never seeking, pausing or changing playback), or the current frame if it is
// paused. Frames are downscaled (long side <= 1280 px) and encoded as JPEG. Nothing is fetched: the pixels come
// from the <video> element already on the page. A cross-origin (tainted) canvas or blank (DRM) frames stop the
// capture and nothing is returned.
(function install(root) {
  'use strict';

  const DEFAULTS = Object.freeze({
    maxFrames: 8,
    intervalMs: 1000,
    maxSide: 1280,
    quality: 0.9,
    fallbackQuality: 0.7,
    maxTotalBytes: 12 * 1024 * 1024, // the server accepts 16 MB; stay well below
    maxFrameBytes: 4 * 1024 * 1024, // the server's per-frame cap
  });
  const HAVE_CURRENT_DATA = 2;
  const PROBE_W = 64; // blank detection runs on a small copy of the frame
  const PROBE_H = 36;

  /** Scale (w, h) down so the long side is at most maxSide; never up. Integer sizes, at least 1 px. */
  function fitSize(width, height, maxSide = DEFAULTS.maxSide) {
    const w = Math.max(0, Number(width) || 0);
    const h = Math.max(0, Number(height) || 0);
    if (!w || !h) return { width: 0, height: 0 };
    const scale = Math.min(1, maxSide / Math.max(w, h));
    return { width: Math.max(1, Math.round(w * scale)), height: Math.max(1, Math.round(h * scale)) };
  }

  /** Mean and standard deviation of luma over RGBA pixel data. */
  function pixelStats(rgba) {
    const n = Math.floor((rgba?.length || 0) / 4);
    if (!n) return { mean: 0, std: 0 };
    let sum = 0;
    let sq = 0;
    for (let i = 0; i < n * 4; i += 4) {
      const y = 0.299 * rgba[i] + 0.587 * rgba[i + 1] + 0.114 * rgba[i + 2];
      sum += y;
      sq += y * y;
    }
    const mean = sum / n;
    return { mean, std: Math.sqrt(Math.max(0, sq / n - mean * mean)) };
  }

  /** True for black or near-uniform frames: what protected (DRM/EME) video draws as, and fades to black. */
  function looksBlank(rgba, threshold = 4) {
    return pixelStats(rgba).std < threshold;
  }

  /** Reading pixels of a cross-origin video throws a SecurityError (the canvas is "tainted"). */
  function isTaintError(err) {
    return Boolean(err) && (err.name === 'SecurityError' || /tainted|insecure/i.test(String(err.message || '')));
  }

  /**
   * Pick the video the user means. `cands`: [{ src, area, visible, playing, ready }] (area = on-screen pixels).
   * The right-clicked video (src match) wins; else the largest visible playing one; else the largest visible one.
   * Returns an index or -1.
   */
  function chooseVideo(cands, srcUrl = '') {
    const usable = cands.map((c, i) => ({ ...c, i })).filter(c => c.visible && c.area > 0);
    if (srcUrl) {
      const hit = usable.find(c => c.src && c.src === srcUrl);
      if (hit) return hit.i;
    }
    const pool = usable.some(c => c.playing) ? usable.filter(c => c.playing) : usable;
    let best = -1;
    let bestArea = 0;
    for (const c of pool) {
      if (c.area > bestArea) {
        best = c.i;
        bestArea = c.area;
      }
    }
    return best;
  }

  /**
   * How many frames to try: 1 when paused or ended; otherwise up to maxFrames, one per interval, but not past the
   * end of a finite video (live streams have an infinite duration).
   */
  function planFrameCount({ paused, ended, duration, currentTime }, maxFrames = DEFAULTS.maxFrames, intervalMs = DEFAULTS.intervalMs) {
    if (paused || ended) return 1;
    const remaining = Number(duration) - Number(currentTime);
    if (Number.isFinite(remaining)) {
      return Math.max(1, Math.min(maxFrames, 1 + Math.floor((remaining * 1000) / intervalMs)));
    }
    return maxFrames;
  }

  /**
   * Payload budget: returns 'ok' (and counts the frame), 'frame_too_large' or 'full'.
   * `budget`: { used, maxTotal, maxFrame }.
   */
  function admitFrame(budget, size) {
    if (size > budget.maxFrame) return 'frame_too_large';
    if (budget.used + size > budget.maxTotal) return 'full';
    budget.used += size;
    return 'ok';
  }

  /**
   * The sampling loop, independent of the DOM. Grabs frame 0 at once, then frame i at start + i * intervalMs
   * (so encoding time does not stretch the schedule) while `stillPlaying()` is true, up to `count` frames.
   * `grab(i)` resolves to { frame } to keep it, { skip: true } to drop it (a blank fade, a repeated frame), or
   * { stop: <code> } to end the capture with an error code. Returns { frames, stop, playbackStopped }.
   */
  async function sampleFrames({ count, intervalMs, grab, stillPlaying, sleep, now = () => Date.now() }) {
    const frames = [];
    let playbackStopped = false;
    const start = now();
    for (let i = 0; i < count; i++) {
      if (i > 0) {
        await sleep(Math.max(0, start + i * intervalMs - now()));
        if (!stillPlaying()) {
          playbackStopped = true;
          break;
        }
      }
      const r = await grab(i);
      if (r?.stop) return { frames, stop: r.stop, playbackStopped };
      if (r?.frame) frames.push(r.frame);
    }
    return { frames, stop: null, playbackStopped };
  }

  function bytesToBase64(bytes) {
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  function describeCandidates(doc, win) {
    const vw = win.innerWidth || 0;
    const vh = win.innerHeight || 0;
    const videos = Array.from(doc.querySelectorAll('video'));
    const cands = videos.map(v => {
      const r = v.getBoundingClientRect();
      const w = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
      const h = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
      const style = win.getComputedStyle ? win.getComputedStyle(v) : null;
      const hidden = style && (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0);
      return {
        src: v.currentSrc || v.src || '',
        area: w * h,
        visible: !hidden && w * h > 0,
        playing: !v.paused && !v.ended && v.readyState > HAVE_CURRENT_DATA,
        ready: v.readyState >= HAVE_CURRENT_DATA,
      };
    });
    return { videos, cands };
  }

  function toBlob(canvas, quality) {
    return new Promise((resolve, reject) => {
      try {
        canvas.toBlob(b => (b ? resolve(b) : reject(new Error('encode failed'))), 'image/jpeg', quality);
      } catch (err) {
        reject(err);
      }
    });
  }

  /**
   * Runs in the page. Returns { ok: true, frames: [{ b64, type, size }], paused, spanS, host, width, height,
   * playbackStopped } or { ok: false, error } with error one of: no_video, not_ready, protected.
   */
  async function capture(options = {}, env = {}) {
    const o = { ...DEFAULTS, ...options };
    const doc = env.document || root.document;
    const win = env.window || root;
    const sleep = env.sleep || (ms => new Promise(r => setTimeout(r, ms)));
    const now = env.now || (() => Date.now());
    const host = String(win.location?.hostname || '');

    const { videos, cands } = describeCandidates(doc, win);
    const index = chooseVideo(cands, o.srcUrl || '');
    if (index < 0) return { ok: false, error: 'no_video', host };
    const video = videos[index];
    if (video.readyState < HAVE_CURRENT_DATA || !video.videoWidth || !video.videoHeight) {
      return { ok: false, error: 'not_ready', host };
    }
    const paused = video.paused || video.ended;
    const count = planFrameCount(video, o.maxFrames, o.intervalMs);
    const size = fitSize(video.videoWidth, video.videoHeight, o.maxSide);
    const canvas = doc.createElement('canvas');
    canvas.width = size.width;
    canvas.height = size.height;
    const ctx = canvas.getContext('2d');
    const probe = doc.createElement('canvas');
    probe.width = PROBE_W;
    probe.height = PROBE_H;
    const pctx = probe.getContext('2d', { willReadFrequently: true });
    const budget = { used: 0, maxTotal: o.maxTotalBytes, maxFrame: o.maxFrameBytes };
    const protectedMedia = Boolean(video.mediaKeys);
    let lastTime = -1;
    let firstAt = null;
    let lastAt = null;
    let full = false;

    const grab = async i => {
      if (full) return { skip: true };
      const t = video.currentTime;
      if (i > 0 && t === lastTime) return { skip: true }; // stalled (buffering): the same picture again
      lastTime = t;
      let blob;
      try {
        ctx.drawImage(video, 0, 0, size.width, size.height);
        pctx.drawImage(canvas, 0, 0, PROBE_W, PROBE_H);
        const blank = looksBlank(pctx.getImageData(0, 0, PROBE_W, PROBE_H).data); // throws when tainted
        if (blank) return protectedMedia ? { stop: 'protected' } : { skip: true };
        blob = await toBlob(canvas, o.quality);
        if (blob.size > o.maxFrameBytes) blob = await toBlob(canvas, o.fallbackQuality);
      } catch (err) {
        return isTaintError(err) ? { stop: 'protected' } : { skip: true };
      }
      const verdict = admitFrame(budget, blob.size);
      if (verdict === 'full') {
        full = true;
        return { skip: true };
      }
      if (verdict !== 'ok') return { skip: true };
      const wall = now();
      firstAt ??= { wall, t };
      lastAt = { wall, t };
      return { frame: { b64: bytesToBase64(new Uint8Array(await blob.arrayBuffer())), type: 'image/jpeg', size: blob.size } };
    };

    const stillPlaying = () => !video.paused && !video.ended && !full;
    const result = await sampleFrames({ count, intervalMs: o.intervalMs, grab, stillPlaying, sleep, now });
    if (result.stop) return { ok: false, error: result.stop, host };
    if (!result.frames.length) return { ok: false, error: 'protected', host };
    let spanS = 0;
    if (firstAt && lastAt) {
      const byVideo = lastAt.t - firstAt.t;
      spanS = byVideo > 0 && byVideo < 60 ? byVideo : (lastAt.wall - firstAt.wall) / 1000;
    }
    return {
      ok: true, frames: result.frames, paused, spanS: Math.round(spanS * 10) / 10, host,
      width: size.width, height: size.height, playbackStopped: result.playbackStopped,
    };
  }

  root.BTFCapture = Object.freeze({
    DEFAULTS, fitSize, pixelStats, looksBlank, isTaintError, chooseVideo, planFrameCount, admitFrame,
    sampleFrames, capture,
  });
})(globalThis);
