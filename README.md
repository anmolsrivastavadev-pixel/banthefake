# BanTheFake

A self-hostable campaign site against harmful deepfakes, with working tools:

- **Campaign site**: a static page (`static/index.html`, `static/app.js`, `static/styles.css`) plus a privacy
  notice (`/privacy.html`, rendered from `app/templates/privacy.html` with the configured contact), with no build step.
- **Media scanner**: upload an image or video. The server checks it for AI-generator markers, IPTC "digital
  source type", EXIF/XMP camera and editing-software metadata, and C2PA Content Credentials, and runs optional
  ML detectors (two models that must agree). The browser also shows a client-side ELA (error level analysis) heatmap.
- **Petition**: sign with name, email and an optional location. Signatures are stored in SQLite, and admins can
  export them or erase them (GDPR).
- **Letter generator**: fill in a few details and copy a ready-to-send letter to a representative. This runs in
  the browser only.

Stack: Python 3.12, FastAPI, Uvicorn and SQLite, managed with [uv](https://docs.astral.sh/uv/).

## What the scanner can and cannot tell you

Read this before you rely on a result.

- **No tool can prove that a depicted scene is real.** A `verified_capture` verdict is a strong positive
  provenance signal: the file has valid Content Credentials from a signer on the official C2PA trust list,
  with a capture claim, and has not changed since the signed history. This proves where the file came from,
  but a camera can still photograph a screen or a staged scene. A `camera` or `unknown` verdict does not mean
  the file is real; it only means no evidence of AI generation was found.

| Verdict | What it is evidence of | What it does not prove |
|---|---|---|
| `verified_capture` | A trusted C2PA signer declares a camera capture, and the bytes are unchanged since signing. | That the scene was real. It can be a photo of a screen or a staged scene. |
| `verified_ai` | A trusted C2PA signer declares generative-AI involvement, and the bytes are unchanged since signing. | Which parts are synthetic, or whether other copies differ. |
| `ai` | AI-generator markers in metadata, or both detector models agreed it looks AI-generated. | That it is definitely AI; markers can be faked and detectors make mistakes. |
| `edited` | Editing software is named, with no AI markers found. | That the edit was harmless, or that the unedited original was real. |
| `camera` | Camera EXIF is present, with no AI markers found. | That the file is real; EXIF can be copied, edited or faked. |
| `unknown` | Nothing conclusive was found either way. | That the file is real. Absence of evidence is not evidence of absence. |

- **Most metadata does not survive sharing.** Social apps and messengers (WhatsApp, Instagram, X, Facebook and
  others) usually strip EXIF, XMP and C2PA data, and anyone can remove it on purpose. If markers are missing,
  that tells you nothing. Markers that are present are strong evidence, but they can be forged too.
- **The ML detector is a heuristic: two models must agree.** Two pinned CPU models score every image and every
  sampled video frame (see [Detector models](#detector-models)). "Likely AI-generated" needs both to agree: the
  primary model at 0.98 or more **and** the secondary model at 0.05 or more. If only one of them is
  suspicious (primary at 0.98 or more, or secondary at 0.5 or more), the result is "Possible signs of AI
  generation". In our benchmark ([`docs/DETECTOR-BENCHMARK.md`](docs/DETECTOR-BENCHMARK.md)), the "likely" rule
  flagged 0 of 157 held-out real images (95% upper bound about 2.4%). It caught 39 of 51 held-out AI images
  (76%), including 24 of 28 Midjourney images. The previous single-model setup caught 57% overall and 18 of 28
  Midjourney images. Zero false positives in a small test does not mean the detector never makes mistakes.
  The test images come from Wikimedia Commons, not social media. Heavy recompression, resizing, screenshots,
  newer generators (GPT-image recall is weak) and deliberate evasion all lower accuracy. A low score never
  means "real". Black-and-white and low-colour images never get an AI verdict from the models, because the
  models are unreliable on them.
- **No face-swap or lip-sync detection.** The detector classifies whole images and sampled video frames (up to
  8 frames spread over the whole clip, with a 20 s decode budget). On videos with keyframes far apart the
  sampler decodes forward in one pass instead of seeking; the report says which part of the clip was covered
  (`model.coverage`), and adds a note when the time budget ran out early. It is not trained to find a face
  swapped into an otherwise real photo or video. A dedicated face detector was researched but not shipped:
  the credible checkpoint has unresolved training-data rights, high CPU/RAM cost, and no openly licensed
  benchmark large enough to establish its false-positive rate. We also measured the best openly licensed
  candidate on 36 real faces: it flagged 92% of them as fake at a 0.5 threshold, so it is nowhere near
  shippable. `docs/LIMITS-PLAN.md` records the gates for revisiting it.
- **Brand names only count where software is named.** A tool name such as "Midjourney" or "Firefly" is only
  treated as a marker in fields that name the creating software: EXIF `Software`/`ProcessingSoftware`, XMP
  `CreatorTool`/history `softwareAgent`, PNG generator keys (`parameters`, `prompt`, `workflow`, `Software`),
  the C2PA claim generator and video `encoder`/`handler` tags. Captions, titles, descriptions, comments and
  copyright notices are ignored. In raw bytes only structured markers are searched: IPTC
  `trainedAlgorithmicMedia`/`compositeWithTrainedAlgorithmicMedia`, Stable Diffusion settings and ComfyUI
  workflows. Each marker signal names the field it came from and quotes up to 80 characters of it.
- **When the detector cannot run, the report says so.** If the detector is enabled but cannot analyze a file
  (undecodable, a non-JPEG image above the decode limit, no decodable video frames, model unavailable), an
  `info` signal "AI detector could not analyze this file (reason)" is added. Truncated or damaged files are
  retried in a tolerant mode and analyzed as far as they can be read, with a note saying so.
- **C2PA trust is checked offline.** The app ships the official C2PA signing and timestamp trust lists. Remote
  manifests and OCSP fetches stay disabled. A cryptographically valid manifest from a signer outside that list
  is reported as untrusted and remains informational; it never drives a verified verdict. Only the signing
  list is loaded as trust anchors: c2pa-python pools every anchor whatever its kind, so loading the TSA list
  would let a timestamp-authority root make a manifest signer "trusted". Timestamps are still validated and
  their trust is still checked against the signing list, but an untrusted timestamp is informational only.
  Claims in an untrusted or invalid manifest (capture, signer, AI declarations) are shown as "Claimed (not
  verified)", never as facts. `c2pa.placed` and `c2pa.unknown` actions count as edits, and the report says how
  many ingredients the credentials include. If an ingredient's own manifest declares generative AI, trusted
  credentials give `verified_ai` and untrusted ones an informational note.
- **Partly synthetic videos.** The clip decision is the tier reached by a majority of the most suspicious half
  of sampled frames, but if any sampled frame is individually rated "likely", the clip is at least "possible"
  and the report says how many frames were. A video with any "likely" or "possible" frame is never described
  as having "no strong AI signal".

## Quickstart

```bash
uv sync                 # core app: metadata, markers and C2PA checks
uv sync --extra ml      # optional: torch + transformers for the ML detector
uv run uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

AI assistants can use the same scanner locally through the stdio MCP server. See
[`docs/MCP.md`](docs/MCP.md) for Claude Code, Codex CLI, Cursor and generic client setup, tool schemas, path
restrictions and interpretation limits.

With the `ml` extra installed, both detector models load in the background at startup (`/api/health` reports
`"model": "loading"`, then `"loaded"`, or `"degraded"` if one of the two models failed to load and the other
runs). On first use they download about 840 MB of weights from the Hugging Face
Hub into `HF_HOME`. Set `BTF_MODEL=off` to disable the detector. Budget about 2.5 GB of RAM for the process
(see [Detector models](#detector-models)).

### Detector models

| Role | Model | Licence | Weights | Pinned revision |
|---|---|---|---:|---|
| Primary (H) | [`haywoodsloan/ai-image-detector-deploy`](https://huggingface.co/haywoodsloan/ai-image-detector-deploy) (SwinV2, AutoTrain) | Apache-2.0 | 745 MB | `4ae1822603cb1ec980c217cfde1ef1b8e0477a16` |
| Secondary (C) | [`buildborderless/CommunityForensics-DeepfakeDet-ViT`](https://huggingface.co/buildborderless/CommunityForensics-DeepfakeDet-ViT) (ViT-S) | MIT | 97 MB | `ac6ee457bea904a373065754107451793b56db00` |

Licences are taken from each model card at the pinned revision. H's training data is not published.

| Decision (per image or frame) | Rule |
|---|---|
| Likely AI-generated | H >= 0.98 and C >= 0.05 |
| Possible signs of AI | otherwise, H >= 0.98 or C >= 0.5 |
| No strong signal | everything else |

How the rule behaves in special cases:

- **Low-colour images** never get "likely".
- **Videos** are decided frame by frame. The clip gets the tier that a majority of its most suspicious half of
  frames reaches: with 8 frames, 3 must be "likely".
- **If one model fails to load,** the other still runs, but the result can at most be "possible". The report
  says which model is missing.
- **`BTF_MODEL_ENSEMBLE=off`** runs only C, with its previous thresholds: 0.85 for "likely" and 0.5 for
  "possible".

The thresholds were chosen on a calibration split and confirmed on a locked test split. Details are in
[`docs/DETECTOR-BENCHMARK.md`](docs/DETECTOR-BENCHMARK.md).

Measured on an Apple M5 (CPU, torch default of 4 threads, warm models):

| | H | C | Both (as shipped) |
|---|---:|---:|---:|
| Per image, median | 252 ms | 73 ms | 332 ms |
| Per image, batch of 8 video frames | 240 ms | 63 ms | about 300 ms |
| Load time (weights cached) | 1.5 s | 1.2 s | 1.6 s |
| Process RSS after first scan | 1.43 GB | 0.64 GB | 1.52 GB |
| Peak process RSS after a batch of 8 | 2.27 GB | 0.70 GB | 2.46 GB |

RSS includes about 0.22 GB for Python, torch and the app. Expect slower inference on small cloud vCPUs.

## Configuration

All settings are environment variables. The defaults below come from `app/config.py` and `app/scan/*.py`.

| Variable | Default | Meaning |
|---|---|---|
| `BTF_DB_PATH` | `data/banthefake.db` (relative to the working directory). The Docker image sets `/data/banthefake.db` | SQLite file for petition signatures. The parent directory is created if missing. |
| `BTF_MAX_UPLOAD_MB` | `50` | Maximum upload size. Enforced while streaming, so larger uploads are aborted with 413. |
| `BTF_UPLOAD_TIMEOUT_S` | `60` | App-side maximum time for receiving and writing an upload. Expiry returns 408 `{"error": "upload_timeout"}` and deletes the partial temp file. The shipped Caddy deployment applies a tighter 35-second request-body timeout. |
| `BTF_SCAN_RATE` | `10` | Scans per **minute** per client IP. Above this you get 429. |
| `BTF_SCAN_CONCURRENCY` | `2` | Scans that may decode media and run the model at the same time (bounds memory). Further scans wait for a slot. |
| `BTF_SCAN_WAIT_S` | `30` | Seconds a fully uploaded scan may wait for a slot before the server answers 503 `{"error": "busy"}` (with `Retry-After`). Queue time is separate from the scan deadline. |
| `BTF_SCAN_ISOLATION` | `on` | Runs every untrusted-media parser and decoder in spawned worker processes. `off`, `0`, `false` or `no` uses the in-process debugging path. Keep this on in production. |
| `BTF_SCAN_TIMEOUT_S` | `30` | Wall-clock scan budget starting after the upload has finished and a concurrency slot is acquired. It covers isolated parsing, parser-pool acquisition, IPC/frame reconstruction and detector inference. An over-budget parser is killed and replaced, and the API returns 504 `{"error": "scan_timeout"}`. |
| `BTF_SCAN_INFER_RESERVE_S` | `3` | Minimum budget that must remain before a detector call starts. With less time, inference is skipped and the report says the detector could not analyze the file because of the time budget. |
| `BTF_SCAN_INFER_BATCH` | `2` | Number of sampled frames sent to the detector per call (1–8). The deadline is checked between calls. |
| `BTF_SCAN_WORKER_JOBS` | `50` | Successful jobs handled by a parser worker before it is recycled. The parser pool size equals `BTF_SCAN_CONCURRENCY`. |
| `BTF_SCAN_MAX_RSS_MB` | `1024` | Address-space/RSS budget per parser worker. Linux enforces `RLIMIT_AS`; all platforms also use a worker RSS watchdog and deterministic pixel/IPC limits. |
| `BTF_SCAN_MAX_NOFILE` | `64` | File-descriptor limit per parser worker (minimum 16). Workers also have a per-job CPU limit. |
| `BTF_SCAN_TEMP_MB` | `64` | Per-worker private temporary-directory quota and `RLIMIT_FSIZE`. The directory is mode 0700, checked by a watchdog and removed when the worker exits. |
| `BTF_SCAN_IPC_MB` | `32` | Maximum parser response size. It covers bounded metadata and raw, downsampled RGB frames returned to the parent for model inference. |
| `BTF_SIGN_RATE` | `5` | Successful petition signatures per **hour** per client IP (honeypot hits count too). Rejected attempts (validation errors, duplicates) do not use this quota. Above it you get 429. |
| `BTF_SIGN_ATTEMPT_RATE` | `30` | All petition sign attempts, valid or not, per **hour** per client IP. Above this you get 429. |
| `BTF_PETITION_GOAL` | `10000` | The `goal` returned by `/api/petition/count`. |
| `BTF_ADMIN_TOKEN` | unset | Bearer token for export and erase. It must be at least 24 characters; a shorter or unset token disables the admin endpoints, which then return 404. |
| `BTF_PRIVACY_CONTACT` | unset | Contact shown on `/privacy.html` for privacy questions and deletion requests: an email address (linked as `mailto:`) or an `https://` URL. Validated at startup: anything else (spaces, quotes, angle brackets, other schemes) stops the app with an error, in every environment. It is substituted into the page server-side and HTML-escaped. If unset outside production, the page shows the placeholder `privacy@YOUR-DOMAIN.example` and a visible "Contact not configured" notice. |
| `BTF_ENV` | `development` | `production` or `development`. In `production` the app refuses to start (logs `Refusing to start: ...` and exits with status 1) while `BTF_PRIVACY_CONTACT` is unset or a placeholder (`YOUR-DOMAIN.example`, or an `example.com/.org/.net`, `.example`, `.test`, `.invalid` or `localhost` domain). Any other value is a startup error. The shipped `.env.example` and `fly.toml` set `production`. |
| `BTF_ADMIN_FAIL_RATE` | `10` | Failed admin authentications allowed per **hour** per client IP. Once the budget is full, all admin requests from that IP return 429 until the window passes, including requests with the correct token. Successful authentication does not use the budget. |
| `BTF_CLIENT_IP_HEADER` | unset | Opt-in name of an operator-trusted header containing exactly one IPv4 or IPv6 client address (for example, `Fly-Client-IP`). The value is used by scan, petition and failed-admin-authentication limits only when this setting is present. Invalid or missing values fall back to the socket peer. Only use a header the edge overwrites. |
| `BTF_HSTS` | unset | `on`, `1`, `true` or `yes` adds `Strict-Transport-Security: max-age=31536000` to every response. Enable it only when HTTPS is enforced for the hostname and the TLS proxy is not already adding HSTS. |
| `BTF_MODEL` | `auto` | `off`, `0`, `false` or `no` disables the ML detector. Any other value enables it when the `ml` extra is installed. |
| `BTF_MODEL_ID` | `haywoodsloan/ai-image-detector-deploy` | Primary detector (H): Hugging Face model id or local path. It must be an image-classification model with a single "AI" logit or a fake/real head. |
| `BTF_MODEL_REVISION` | pinned commit for the default model; unset for a custom model | Exact Hugging Face commit/tag for the primary model. Set this when using a remote custom model so deployments are reproducible. |
| `BTF_MODEL2_ID` | `buildborderless/CommunityForensics-DeepfakeDet-ViT` | Secondary detector (C), same requirements as `BTF_MODEL_ID`. |
| `BTF_MODEL2_REVISION` | pinned commit for the default model; unset for a custom model | Exact commit/tag for the secondary model. |
| `BTF_MODEL_ENSEMBLE` | `on` | `off`, `0`, `false` or `no` runs only the secondary model with its previous thresholds (likely >= 0.85, possible >= 0.5). Otherwise both models run and "likely" needs both to agree. The thresholds were calibrated for the default pair; custom models need their own calibration. |
| `BTF_MODEL_THREADS` | unset (torch default) | Integer passed to `torch.set_num_threads`. |
| `BTF_MAX_IMAGE_PIXELS` | `120000000` | Images larger than this many pixels are refused (decompression-bomb guard). |
| `BTF_MAX_DECODE_PIXELS` | `25000000` | Largest image decoded at full size for the detector. JPEGs above it are decoded at 1/2, 1/4 or 1/8 scale; other formats above it are not analyzed by the detector (the report says so). |
| `BTF_C2PA_TRUST` | `official` | `official` uses the bundled C2PA signing trust list (see "C2PA trust is checked offline" for why the TSA list is not loaded). `off` disables signer trust, so no manifest receives a verified verdict. |
| `BTF_C2PA_TRUST_ANCHORS` | unset | Path to a PEM file of trusted C2PA signer roots. By default this **replaces** the official list, so official signers (for example Google Pixel) are no longer trusted. Trusted results then report `trust_list_version: "custom"`. If the file is missing, unreadable or contains no PEM certificate, an error is logged and **no** signer is trusted (it never silently falls back to the official list). |
| `BTF_C2PA_TRUST_ANCHORS_MODE` | `replace` | `append` trusts the official list **and** the `BTF_C2PA_TRUST_ANCHORS` roots; trusted results then report `trust_list_version: "<official date>+custom"`. |
| `HF_HOME` | `~/.cache/huggingface`. The Docker image sets `/opt/hf` | Hugging Face cache for the detector weights. Set `HF_HUB_OFFLINE=1` to forbid downloads. |

## API

All responses are JSON unless noted. Errors look like `{"error": "<message>"}` with a matching status code,
including unexpected server errors (500 `{"error": "Internal server error."}`).
Every response, error responses included, carries a strict CSP, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: strict-origin-when-cross-origin` and `X-Frame-Options: DENY`. With `BTF_HSTS=on`, it also
carries `Strict-Transport-Security: max-age=31536000`.

| Method and path | Body | Success | Errors |
|---|---|---|---|
| `GET /api/health` | none | 200 `{"ok": true, "model": "loaded"\|"degraded"\|"loading"\|"off"\|"unavailable", "c2pa": bool}` (`degraded`: one of the two detector models failed to load) | none |
| `POST /api/scan` | `multipart/form-data`, field `file` | 200 scan report (below) | 400 (not multipart, no `file` field, empty, malformed or unterminated upload, client disconnected), 408 `upload_timeout`, 413 too large, 415 unsupported type (also audio-only MP4), 429 rate limited, 503 `busy` (no scan slot within `BTF_SCAN_WAIT_S`), 504 `scan_timeout`, 500 `scan_failed` |
| `POST /api/petition` | `application/json`, at most 16 KB: `{"name", "email", "location"?, "consent": true, "website"?}` | 201 `{"ok": true}` | 400 body is not a JSON object, 409 `already_signed`, 413 body over 16 KB, 415 not `application/json`, 422 validation, 429 rate limited |
| `GET /api/petition/count` | none | 200 `{"count": int, "goal": int}` | none |
| `GET /api/petition/export.csv` | admin bearer token | 200 `text/csv` (`name,email,location,created_at`) | 401 wrong or missing token, 404 admin disabled, 429 failed-authentication limit reached |
| `DELETE /api/petition` | admin bearer token, `application/json` (at most 16 KB) `{"email": "..."}` | 200 `{"deleted": true}` | 401 wrong or missing token, 404 admin disabled or `not_found`, 413 body over 16 KB, 415 not `application/json`, 422 email missing, 429 failed-authentication limit reached |

Notes:

- **Scan input** is identified by magic bytes, and the client content type is ignored. The accepted types
  are JPEG, PNG, WEBP, GIF, HEIC/HEIF (only if this server's Pillow can open them) and MP4, MOV and WEBM.
- **Media parsing is process-isolated.** Raw marker scans, Pillow/EXIF/XMP/PNG parsing, PyAV opening, seeking
  and decoding, and C2PA validation run in spawned workers. The parent kills and replaces a worker that hangs,
  crashes, is cancelled or exceeds its limits. Workers return a strictly validated JSON header followed by
  bounded RGB frame bytes; the parent never unpickles worker output. Detector models remain loaded once in the
  parent. See [`docs/ISOLATION-DESIGN.md`](docs/ISOLATION-DESIGN.md) for the boundary and IPC contract. A detector
  call already executing in the parent cannot be interrupted; a hard per-call inference bound would require a
  separately killable inference process.
- **Petition validation**: name must be 1 to 100 characters, email must be valid and at most 254 characters,
  location must be at most 100 characters, and `consent` must be `true`. Emails are trimmed and lowercased, and
  each email can sign once. Emails must be ASCII, with no leading, trailing or consecutive dots in the local
  part and domain labels that do not start or end with `-`. Names and locations are NFC-normalized; control
  characters become spaces and zero-width/bidi format characters are removed; a name with nothing visible left
  is rejected. `website` is a honeypot field: if it is non-empty, the server returns 201 and stores nothing.
- **Petition requests must be `application/json`.** Cross-site HTML forms can only send `text/plain`,
  urlencoded or multipart bodies without a CORS preflight, so this blocks form-based CSRF signatures.
- **CSV export** cells starting with `=`, `+`, `-`, `@`, tab or CR are prefixed with `'` to prevent formula
  injection.

Example scan report:

```json
{
  "verdict": "ai",
  "verdict_title": "Likely AI-generated",
  "verdict_detail": "... No tool can prove a file is real.",
  "signals": [
    {"kind": "ai", "text": "Metadata names Midjourney. Found in EXIF Software: \u201cMidjourney v6\u201d",
     "weight": 0.9, "field": "EXIF Software", "snippet": "Midjourney v6"},
    {"kind": "model", "text": "...", "weight": 0.99}
  ],
  "c2pa": null,
  "model": {
    "name": "haywoodsloan/ai-image-detector-deploy + CommunityForensics-DeepfakeDet-ViT (agreement)",
    "ai_probability": 0.9999,
    "decision": "likely",
    "models": [
      {"name": "haywoodsloan/ai-image-detector-deploy", "ai_probability": 0.9999},
      {"name": "CommunityForensics-DeepfakeDet-ViT", "ai_probability": 0.9941}
    ],
    "frames_checked": 1
  },
  "file": {"type": "image", "mime": "image/jpeg", "size_bytes": 123456, "width": 1024, "height": 1024}
}
```

`verdict` is one of `verified_capture`, `verified_ai`, `ai`, `edited`, `camera` or `unknown`.
`verified_capture` requires a fully valid manifest from a trusted signer with a `c2pa.created` action whose
digital source type is `digitalCapture` or `computationalCapture`, and no generative-AI source type.
`verified_ai` requires a fully valid, trusted manifest declaring trained-algorithmic media, itself or in an
ingredient's own manifest. Valid but
untrusted manifests remain informational. The `kind` of a signal is one of `ai`, `edit`,
`camera`, `c2pa`, `model` or `info`. Marker signals (`ai`, `edit`) also have `field` (where the marker was
found) and `snippet` (the matched text, at most 80 characters). When present, `c2pa` has the fields `present`,
`valid`, `trusted`, `generator`, `issuer`, `signer`, `signed_at`, `capture`, `claimed_capture`,
`ai_generated`, `ingredient_ai`, `actions`, `edits`, `ingredients` and `trust_list_version`. `capture` is true
only for a valid manifest from a trusted signer; `claimed_capture` is the manifest's own claim, verified or not.
`ai_generated` also covers an AI declaration in an ingredient's manifest (`ingredient_ai`); such ingredients
carry `"ai_generated": "true"`. Generator, signer and signing time come from the manifest and are only
verified when `valid` and `trusted` are both true.

When present, `model` has these fields:

- `decision` (`likely`, `possible` or `none`) is the combined result that drives the verdict.
- `models` lists every model that ran, primary first, as `{name, ai_probability}`.
- `low_colour: true` is present when the image is black-and-white or low-colour: the scores are then shown for
  reference only and never indicate either way.
- `ai_probability` is kept for compatibility. It is the **primary** model's probability, or the only model's
  when just one ran (one failed to load, or `BTF_MODEL_ENSEMBLE=off`). Do not threshold it yourself; use
  `decision`.

For videos, each `ai_probability` is the mean of that model's most suspicious half of frames. `model` also has
`frame_probabilities` (primary model), `frame_timestamps`, `coverage` (`first_s`, `last_s`, `duration_s`), and
`frames_likely` / `frames_possible` (sampled frames individually rated "likely" / "possible").
Each `models` entry also has its own `frame_probabilities`.

### Updating the C2PA trust list

The bundled trust material is in `app/trust/`. Its current issue date is **2026-10-23** and its next-update
date is **2027-01-21**. To update it:

1. Obtain the current signing list, timestamp list and `store.cfg` from
   <https://c2pa.org/conformance>.
2. Check the issue and next-update fields, then replace all three files together. Update
   `app/trust/VERSION` and `app/trust/README.md` with the new dates.
3. Run both test suites and verify that an official sample is trusted, the C2PA test certificate remains
   untrusted, a tampered sample is invalid, and `BTF_C2PA_TRUST=off` trusts nothing.

The research-only `anchors.pem` is not shipped because its source and redistribution terms have not been
confirmed.

### Admin examples

```bash
export BTF_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"   # save this value

# Export all signatures
curl -fsS -H "Authorization: Bearer $BTF_ADMIN_TOKEN" \
  https://example.org/api/petition/export.csv -o signatures.csv

# Erase one signature (GDPR erasure request)
curl -fsS -X DELETE -H "Authorization: Bearer $BTF_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"email": "person@example.org"}' \
  https://example.org/api/petition
```

## Privacy

- **Uploads**: each upload is streamed to a temp file, scanned, and deleted in a `finally` block, so it is
  removed even when upload receipt times out, the scan fails, its parser crashes, or its deadline expires.
  Upload receipt has its own `BTF_UPLOAD_TIMEOUT_S` bound; the shipped Caddy proxy also enforces `read_body 35s`.
  Uploads are never stored or logged. Parser workers only read the temporary file. The ML model runs locally,
  so no third-party API sees it.
- **Petition**: the server stores name, email, optional location and a UTC timestamp. It does **not** store IP
  addresses. IPs are only held in memory by the rate limiter, for its sliding window.
- **Access logs**: Uvicorn's access log prints client IPs. The Docker image runs with `--no-access-log`. If you
  run Uvicorn yourself, pass that flag too, or configure your reverse proxy's logs to match your privacy policy.
  Failed admin authentication warnings contain only an IPv4 `/24` or IPv6 `/48` network prefix and never the
  supplied token.
- **Third parties**: the page loads Google Fonts (allowed by the CSP). Self-host the fonts if you want no
  third-party requests.
- **Privacy notice**: `/privacy.html` describes the behaviour above to visitors and is linked from the
  petition consent text and the footer. The page is rendered from `app/templates/privacy.html` with the
  operator's contact substituted. **Before launch, set `BTF_PRIVACY_CONTACT`** to a real, monitored inbox (or
  an `https://` contact page) and `BTF_ENV=production`, which refuses to start without one. Review the page
  against your own hosting setup (proxy or host logs) and any legal advice you have. Deletion requests
  sent there are handled with the admin `DELETE /api/petition` call shown above. If you self-host the fonts,
  update the "Third parties" section of the page.

## Deployment

For a step-by-step production setup (hosting options and costs, a VPS with Docker Compose and Caddy, or
Fly.io, plus backups, updates and a pre-launch checklist), see [`docs/DEPLOY.md`](docs/DEPLOY.md).

### Docker

```bash
docker build -t banthefake .
docker run -d --name banthefake -p 8000:8000 \
  -v banthefake-data:/data \
  -e BTF_ADMIN_TOKEN='replace-with-at-least-24-random-characters' \
  -e BTF_PRIVACY_CONTACT='privacy@your-domain.org' -e BTF_ENV=production \
  -e FORWARDED_ALLOW_IPS='10.0.0.2' \
  banthefake
curl http://127.0.0.1:8000/api/health
```

The image works like this:

- It is based on `python:3.12-slim` and installs the locked dependencies with uv, including the `ml` extra.
- It runs as a non-root user (`btf`, uid 10001), listens on port 8000, and stores the database at
  `/data/banthefake.db` on a `VOLUME`.
- **CPU-only torch.** `uv.lock` pins `torch` from PyPI, and on Linux that pulls in several GB of CUDA packages.
  The Dockerfile installs every locked package except `torch`, `nvidia-*`, `cuda-*` and `triton`, then
  installs the same torch version from the PyTorch CPU index (`torch==<locked>+cpu`). `pyproject.toml` and
  `uv.lock` are not changed. To make this the declared behaviour for all Linux installs, add a
  `[tool.uv.sources]` entry pointing `torch` at a `https://download.pytorch.org/whl/cpu` index and re-lock.
- **c2pa-python** is pinned to `0.38.1` in `pyproject.toml`, which has Linux (manylinux 2.28, x86_64 and
  aarch64) and macOS wheels, and installs straight from the lock. (`0.38.2` was published with macOS wheels
  only, so do not bump past it without checking that Linux wheels exist.)
- **Model weights.** Both pinned detectors (about 840 MB) are downloaded at build time into `HF_HOME=/opt/hf`,
  and the container runs with `HF_HUB_OFFLINE=1`, so it needs no network at runtime. Use
  `--build-arg BTF_MODEL_ID=... --build-arg BTF_MODEL_REVISION=...` (or `BTF_MODEL2_ID`/`BTF_MODEL2_REVISION`)
  to bake in different models.
- **Starting the server.** The CMD runs the venv's `uvicorn` directly. Do not use `uv run` inside the image,
  because it would re-sync and reinstall CUDA torch.
- **Parser limits.** Linux containers enforce `RLIMIT_AS` for parser workers. macOS does not reliably enforce
  that limit, so the app also checks worker RSS and always applies pre-decode pixel limits, bounded frame sizes,
  a 32 MiB IPC cap and the hard wall-clock deadline. Size the container for the parent model memory plus
  `BTF_SCAN_CONCURRENCY * BTF_SCAN_MAX_RSS_MB` of parser headroom.

### Behind a reverse proxy

Run Uvicorn with `--proxy-headers --forwarded-allow-ips <proxy-ip>`. In Docker, set the `FORWARDED_ALLOW_IPS`
environment variable, which Uvicorn reads. Without this, every request appears to come from the proxy's IP, so
all visitors share one rate-limit bucket. Do not set `*` unless the app is reachable only through the proxy,
because clients could then spoof `X-Forwarded-For` to bypass rate limits. Also configure the proxy to:

- allow request bodies slightly larger than `BTF_MAX_UPLOAD_MB`;
- allow a read timeout long enough for video scans;
- terminate TLS.

When a managed edge supplies a dedicated, overwritten client-IP header, set `BTF_CLIENT_IP_HEADER` to that
header name instead. The app accepts only a single valid IPv4 or IPv6 value and otherwise uses the socket
peer. This is opt-in because trusting a client-controlled header lets visitors evade rate limits.

### Single process

The rate limiter (`app/ratelimit.py`) keeps its state **in memory, per process**. With `--workers N` or several
replicas, each process has its own counters, so the effective limit becomes N times the configured value. Run
a single worker (the image does), or replace the limiter with a shared store such as Redis, or enforce limits
at the proxy. One process is enough for moderate traffic: at most `BTF_SCAN_CONCURRENCY` scans run at a time,
backed by the same number of parser workers, and inference is serialised per model. Scan slots, parser pools and
loaded models are per web process, so `--workers N` multiplies all three as well as the rate-limit issue above.

### Persistence

Signatures live in the SQLite file at `BTF_DB_PATH`. Mount a persistent volume there, `/data` in Docker, and
back it up. Without a volume, signatures are lost when the container is removed.

## Running tests

```bash
uv sync --extra ml          # the ml extra is only needed for the ml-marked tests
uv run pytest -q            # all tests. ml tests are skipped if torch/transformers are missing
uv run pytest -q -m ml      # only the detector tests (downloads the weights on first run)
uv run pytest -q -m "not ml"
```

### Test fixture licences

Sources, authors, licences, modifications, and derived-file details are recorded in
[`tests/fixtures/ATTRIBUTION.md`](tests/fixtures/ATTRIBUTION.md). `.dockerignore` keeps `tests/` out of the
Docker image.
