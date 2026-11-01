# BanTheFake: product spec

Campaign site + working tools against harmful deepfakes. Turns the static prototype
(`static/index.html`, originally `index.html`) into a real, self-hostable app.

## Stack
- Python + FastAPI + Uvicorn, managed with `uv` (`pyproject.toml`, `uv.lock`).
- SQLite (stdlib `sqlite3`) for petition signatures. DB path from `BTF_DB_PATH` (default `data/banthefake.db`).
- Frontend: the single `static/index.html` (vanilla JS, no build step), served by FastAPI.
- Tests: `pytest` + FastAPI `TestClient`. `uv run pytest` must pass.

## Layout
```
app/
  main.py          FastAPI app factory `create_app()`, static mount, security headers, routes
  config.py        settings from env vars
  ratelimit.py     in-memory sliding-window limiter keyed by client IP
  petition.py      petition storage + validation
  scan/
    __init__.py    `scan_file(path, filename, content_type) -> ScanReport`
    markers.py     raw-byte marker scan (AI generators, editors, IPTC digital source type)
    exif.py        EXIF/XMP extraction (camera make/model, software, dates)
    c2pa_check.py  C2PA manifest reading + validation (optional dependency; degrade gracefully)
    isolate.py     spawned parser-worker pool, hard deadlines, resource limits and bounded IPC
    model.py       optional ML classifier, lazy-loaded, disabled when `BTF_MODEL=off`
    video.py       frame sampling for video files
static/index.html
tests/
```

## API
All JSON. Errors: `{"error": "<message>"}` with proper status code.

### `POST /api/scan` (multipart, field `file`)
- Max size `BTF_MAX_UPLOAD_MB` (default 50) → 413 when exceeded (enforce while streaming, not after).
- Upload receipt is bounded by `BTF_UPLOAD_TIMEOUT_S` (default 60 s) → 408 `{"error": "upload_timeout"}`;
  the partial temp file is deleted. Production Caddy applies a tighter 35 s request-body timeout.
- Accept only images (jpeg, png, webp, gif, heic/heif if Pillow can open) and video (mp4, mov, webm). Sniff
  magic bytes; never trust the client content-type alone → 415 otherwise.
- Upload written to a temp file, always deleted after scan (even on error). Never persisted or logged.
- Rate limit: `BTF_SCAN_RATE` per minute per IP (default 10) → 429.
- All byte interpretation (marker scanning, Pillow/EXIF/XMP/PNG, PyAV and C2PA) runs in a spawned parser
  worker by default (`BTF_SCAN_ISOLATION=on`). The pool size equals `BTF_SCAN_CONCURRENCY`; workers recycle
  after `BTF_SCAN_WORKER_JOBS` (default 50).
- A fully received upload waits at most `BTF_SCAN_WAIT_S` for an API scan slot, then returns 503 `busy`. After
  acquiring the slot, the parser job gets a fresh hard `BTF_SCAN_TIMEOUT_S` deadline (default 30 s). Timeout
  kills and replaces the worker and returns 504 `{"error": "scan_timeout"}`; a worker crash returns 500
  `{"error": "scan_failed"}` and the pool recovers. The route's `finally` still deletes the upload.
- Workers apply `RLIMIT_AS`/RSS, CPU, file-size and descriptor limits plus bounded pixel/frame/IPC budgets.
  Non-JPEG images above `BTF_MAX_DECODE_PIXELS` keep metadata/C2PA results but are not pixel-decoded for the
  model. JPEG uses libjpeg shrink-on-load.
- Detector inference stays in the parent so both models load once. Workers return at most 32 MiB of validated,
  downsampled RGB frame bytes; successful isolated and in-process reports have identical shape and content.
- Response:
```json
{
  "verdict": "verified_capture" | "verified_ai" | "ai" | "edited" | "camera" | "unknown",
  "verdict_title": "...", "verdict_detail": "...",
  "signals": [{"kind": "ai|edit|camera|c2pa|model|info", "text": "...", "weight": 0.0-1.0}],
  "c2pa": null | {"present": true, "valid": bool, "trusted": bool, "generator": str, "issuer": str,
    "signer": str, "signed_at": str, "capture": bool, "ai_generated": bool, "actions": [...],
    "edits": [...], "ingredients": [...], "trust_list_version": str},
  "model": null | {"name": str, "ai_probability": float, "frames_checked": int},
  "file": {"type": "image|video|video_frames", "mime": str, "size_bytes": int, "width": int?, "height": int?,
    "frames_checked": int?}
}
```
- Verdict rules: a fully valid, officially trusted C2PA camera-capture claim → `verified_capture`; a fully
  valid, officially trusted generative-AI claim → `verified_ai`. Valid but untrusted claims are informational.
  Otherwise C2PA or IPTC says AI, or generator markers → `ai`. Model probability ≥ 0.85 → `ai` with
  wording "likely". Editing software only → `edited`. Camera EXIF and nothing else → `camera`. Else `unknown`.
  Capture copy states that provenance does not prove the depicted scene itself is real.

### `POST /api/scan-frames` (multipart, 1–16 parts named `frames`)
- For the browser extension's video check: still frames captured in the user's browser from a `<video>` they are
  playing, in playback order. The server never fetches videos or URLs (no platform downloading, no SSRF surface).
- Each part must sniff as JPEG, PNG or WEBP → 415 otherwise. At most 16 parts, 4 MB per part and 16 MB in total
  (also ≤ `BTF_MAX_UPLOAD_MB`) → 413, enforced while streaming. Parts are written to a per-request temp directory
  that is always deleted.
- Same rate limit (shares `BTF_SCAN_RATE` with `/api/scan`), CORS opt-in list, scan slot, `BTF_SCAN_WAIT_S`,
  hard deadline and isolated parsing as `/api/scan` (one parser job per frame within one deadline).
- Frame metadata, C2PA and markers are ignored (stated in a signal). Frames are downscaled to 1024 px like sampled
  video frames, decided per frame and aggregated with the video rule (majority of the most suspicious half; any
  "likely" frame → at least "possible"). Undecodable frames are skipped and reported; none decodable → 415.
- Response: the normal scan report with `c2pa: null` and `file: {"type": "video_frames", "mime", "size_bytes",
  "frames_checked", "width"?, "height"?}`, plus an `info` signal: frames were captured from a playing video in the
  browser; screen capture and re-compression reduce detector reliability; audio is not analysed; "No tool can
  prove a file is real."

### Petition
- `POST /api/petition` `{name, email, location?, consent: true, website?}`
  - `website` is a honeypot: if non-empty, return 201 but do not store.
  - Validate: name 1–100 chars, email syntactically valid and ≤254, location ≤100, consent must be true.
  - Email normalized (trim + lowercase) and unique → 409 `already_signed`.
  - Rate limit `BTF_SIGN_RATE` per hour per IP (default 5).
  - Stores name, email, location, created_at (UTC ISO). Does not store IP.
- `GET /api/petition/count` → `{"count": int, "goal": int}` (goal from `BTF_PETITION_GOAL`, default 10000).
- `GET /api/petition/export.csv` — requires `Authorization: Bearer $BTF_ADMIN_TOKEN`; 404 if token unset,
  401 if wrong (constant-time compare). CSV-injection safe (prefix `'` to cells starting with = + - @).
- `DELETE /api/petition` `{email}` with admin token: remove a signature (GDPR erasure).

### Misc
- `GET /api/health` → `{"ok": true, "model": "loaded|off|unavailable", "c2pa": bool}`.
- Security headers on all responses: CSP (self + Google Fonts), X-Content-Type-Options, Referrer-Policy,
  X-Frame-Options DENY.
- CORS is off by default. `BTF_CORS_ORIGINS` (exact origins, comma-separated) lets those origins read
  `POST /api/scan`, `POST /api/scan-frames` and `GET /api/health` (incl. preflight); no credentials, no other routes. Used by the
  browser extension (`extension/`, `docs/EXTENSION.md`) when it has no host permission for the server.

## Frontend changes
- Scanner posts to `/api/scan`, shows verdict/signals/C2PA/model results, keeps client-side ELA heatmap.
  Loading state, error states (413/415/429/network).
- Petition posts to `/api/petition` with consent checkbox + hidden honeypot; count from `/api/petition/count`.
- No inline event handlers; inline `<script>` is fine only if CSP allows it — prefer moving JS to `static/app.js`
  and CSS to `static/styles.css` so CSP can be strict (`script-src 'self'`).

## Ops
- `README.md`: run locally, env vars, deploy notes, limits.
- `Dockerfile` (python slim, uv), non-root user.
