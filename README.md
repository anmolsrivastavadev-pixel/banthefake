# BanTheFake

A self-hostable campaign site against harmful deepfakes, with working tools:

- **Campaign site**: a single static page (`static/index.html`, `static/app.js`, `static/styles.css`), with no build step.
- **Media scanner**: upload an image or video. The server checks it for AI-generator markers, IPTC "digital
  source type", EXIF/XMP camera and editing-software metadata, and C2PA Content Credentials, and runs an optional
  ML detector. The browser also shows a client-side ELA (error level analysis) heatmap.
- **Petition**: sign with name, email and an optional location. Signatures are stored in SQLite, and admins can
  export them or erase them (GDPR).
- **Letter generator**: fill in a few details and copy a ready-to-send letter to a representative. This runs in
  the browser only.

Stack: Python 3.12, FastAPI, Uvicorn and SQLite, managed with [uv](https://docs.astral.sh/uv/).

## What the scanner can and cannot tell you

Read this before you rely on a result.

- **No tool can prove that a file is authentic.** Every verdict says so. A "camera" or "unknown" verdict does
  not mean the file is real. It only means no evidence of AI generation was found.
- **Most metadata does not survive sharing.** Social apps and messengers (WhatsApp, Instagram, X, Facebook and
  others) usually strip EXIF, XMP and C2PA data, and anyone can remove it on purpose. If markers are missing,
  that tells you nothing. Markers that are present are strong evidence, but they can be forged too.
- **The ML detector is a heuristic.** The default model is
  [`buildborderless/CommunityForensics-DeepfakeDet-ViT`](https://huggingface.co/buildborderless/CommunityForensics-DeepfakeDet-ViT)
  (MIT licence, about 87 MB, runs on CPU). It outputs P(AI-generated). A score of 0.85 or more gives a
  "Likely AI-generated" verdict, and 0.5 to 0.85 gives "Possible signs of AI generation". It is good on many
  diffusion and GAN images but borderline on some, such as some Midjourney images (the
  `ai_midjourney_city.jpg` fixture scores in the "possible" band). Heavy recompression, resizing,
  screenshots and new generators all lower its accuracy. A low score never means "real".
- **No face-swap or lip-sync detection.** The detector classifies whole images and sampled video frames (up to
  8 frames per video, with a 20 s decode budget). It is not trained to find a face swapped into an otherwise
  real photo or video.
- **C2PA signer trust needs configuration.** Manifests are read and their cryptographic validity is checked
  offline (no remote manifest or OCSP fetch). Whether the *signer* is trusted depends on the trust anchors you
  provide in `BTF_C2PA_TRUST_ANCHORS`. Without them, valid manifests are reported as `"trusted": false`.

## Quickstart

```bash
uv sync                 # core app: metadata, markers and C2PA checks
uv sync --extra ml      # optional: torch + transformers for the ML detector
uv run uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>.

With the `ml` extra installed, the detector loads in the background at startup (`/api/health` reports
`"model": "loading"`, then `"loaded"`). On first use it downloads the weights (about 87 MB) from the Hugging
Face Hub into `HF_HOME`. Set `BTF_MODEL=off` to disable it.

## Configuration

All settings are environment variables. The defaults below come from `app/config.py` and `app/scan/*.py`.

| Variable | Default | Meaning |
|---|---|---|
| `BTF_DB_PATH` | `data/banthefake.db` (relative to the working directory). The Docker image sets `/data/banthefake.db` | SQLite file for petition signatures. The parent directory is created if missing. |
| `BTF_MAX_UPLOAD_MB` | `50` | Maximum upload size. Enforced while streaming, so larger uploads are aborted with 413. |
| `BTF_SCAN_RATE` | `10` | Scans per **minute** per client IP. Above this you get 429. |
| `BTF_SIGN_RATE` | `5` | Petition sign attempts per **hour** per client IP. Above this you get 429. |
| `BTF_PETITION_GOAL` | `10000` | The `goal` returned by `/api/petition/count`. |
| `BTF_ADMIN_TOKEN` | unset | Bearer token for export and erase. While it is unset, the admin endpoints return 404. |
| `BTF_MODEL` | `auto` | `off`, `0`, `false` or `no` disables the ML detector. Any other value enables it when the `ml` extra is installed. |
| `BTF_MODEL_ID` | `buildborderless/CommunityForensics-DeepfakeDet-ViT` | Hugging Face model id or local path for the detector. It must be an image-classification model with a single "AI" logit or a fake/real head. |
| `BTF_MODEL_THREADS` | unset (torch default) | Integer passed to `torch.set_num_threads`. |
| `BTF_MAX_IMAGE_PIXELS` | `120000000` | Images larger than this many pixels are refused (decompression-bomb guard). |
| `BTF_C2PA_TRUST_ANCHORS` | unset | Path to a PEM file of trusted C2PA signing roots. Without it, no signer is reported as trusted. |
| `HF_HOME` | `~/.cache/huggingface`. The Docker image sets `/opt/hf` | Hugging Face cache for the detector weights. Set `HF_HUB_OFFLINE=1` to forbid downloads. |

## API

All responses are JSON unless noted. Errors look like `{"error": "<message>"}` with a matching status code.
Every response carries a strict CSP, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: strict-origin-when-cross-origin` and `X-Frame-Options: DENY`.

| Method and path | Body | Success | Errors |
|---|---|---|---|
| `GET /api/health` | none | 200 `{"ok": true, "model": "loaded"\|"loading"\|"off"\|"unavailable", "c2pa": bool}` | none |
| `POST /api/scan` | `multipart/form-data`, field `file` | 200 scan report (below) | 400 (not multipart, no `file` field, empty or malformed upload), 413 too large, 415 unsupported type, 429 rate limited, 500 scan failed |
| `POST /api/petition` | `{"name", "email", "location"?, "consent": true, "website"?}` | 201 `{"ok": true}` | 400 body is not a JSON object, 409 `already_signed`, 422 validation, 429 rate limited |
| `GET /api/petition/count` | none | 200 `{"count": int, "goal": int}` | none |
| `GET /api/petition/export.csv` | admin bearer token | 200 `text/csv` (`name,email,location,created_at`) | 401 wrong or missing token, 404 admin disabled |
| `DELETE /api/petition` | admin bearer token, `{"email": "..."}` | 200 `{"deleted": true}` | 401 wrong or missing token, 404 admin disabled or `not_found`, 422 email missing |

Notes:

- **Scan input** is identified by magic bytes, and the client content type is ignored. The accepted types
  are JPEG, PNG, WEBP, GIF, HEIC/HEIF (only if this server's Pillow can open them) and MP4, MOV and WEBM.
- **Petition validation**: name must be 1 to 100 characters, email must be valid and at most 254 characters,
  location must be at most 100 characters, and `consent` must be `true`. Emails are trimmed and lowercased, and
  each email can sign once. `website` is a honeypot field: if it is non-empty, the server returns 201 and stores
  nothing.
- **CSV export** cells starting with `=`, `+`, `-`, `@`, tab or CR are prefixed with `'` to prevent formula
  injection.

Example scan report:

```json
{
  "verdict": "ai",
  "verdict_title": "Likely AI-generated",
  "verdict_detail": "... No tool can prove a file is real.",
  "signals": [{"kind": "model", "text": "...", "weight": 0.99}],
  "c2pa": null,
  "model": {"name": "CommunityForensics-DeepfakeDet-ViT", "ai_probability": 0.9941, "frames_checked": 1},
  "file": {"type": "image", "mime": "image/jpeg", "size_bytes": 123456, "width": 1024, "height": 1024}
}
```

`verdict` is one of `ai`, `edited`, `camera` or `unknown`. The `kind` of a signal is one of `ai`, `edit`,
`camera`, `c2pa`, `model` or `info`. When present, `c2pa` has the fields `present`, `valid`, `trusted`,
`generator`, `issuer`, `ai_generated` and `actions`.

### Admin examples

```bash
export BTF_ADMIN_TOKEN='a-long-random-secret'   # set the same value on the server

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
  removed even when the scan fails. Uploads are never stored or logged. The ML model runs locally, so no
  third-party API sees them.
- **Petition**: the server stores name, email, optional location and a UTC timestamp. It does **not** store IP
  addresses. IPs are only held in memory by the rate limiter, for its sliding window.
- **Access logs**: Uvicorn's access log prints client IPs. The Docker image runs with `--no-access-log`. If you
  run Uvicorn yourself, pass that flag too, or configure your reverse proxy's logs to match your privacy policy.
- **Third parties**: the page loads Google Fonts (allowed by the CSP). Self-host the fonts if you want no
  third-party requests.

## Deployment

### Docker

```bash
docker build -t banthefake .
docker run -d --name banthefake -p 8000:8000 \
  -v banthefake-data:/data \
  -e BTF_ADMIN_TOKEN='a-long-random-secret' \
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
- **c2pa-python workaround.** The locked `c2pa-python==0.38.2` was published with macOS wheels only, so a plain
  `uv sync --frozen` fails on Linux. The Dockerfile tries the locked version first and falls back to `0.38.1`
  (build arg `C2PA_FALLBACK`), which has Linux wheels and passes the C2PA test suite. Fix it properly by
  re-locking with a version that ships Linux wheels.
- **Model weights.** The detector weights are downloaded at build time into `HF_HOME=/opt/hf`, and the
  container runs with `HF_HUB_OFFLINE=1`, so it needs no network at runtime. Use `--build-arg BTF_MODEL_ID=...`
  to bake in a different model.
- **Starting the server.** The CMD runs the venv's `uvicorn` directly. Do not use `uv run` inside the image,
  because it would re-sync and reinstall CUDA torch.

### Behind a reverse proxy

Run Uvicorn with `--proxy-headers --forwarded-allow-ips <proxy-ip>`. In Docker, set the `FORWARDED_ALLOW_IPS`
environment variable, which Uvicorn reads. Without this, every request appears to come from the proxy's IP, so
all visitors share one rate-limit bucket. Do not set `*` unless the app is reachable only through the proxy,
because clients could then spoof `X-Forwarded-For` to bypass rate limits. Also configure the proxy to:

- allow request bodies slightly larger than `BTF_MAX_UPLOAD_MB`;
- allow a read timeout long enough for video scans;
- terminate TLS.

### Single process

The rate limiter (`app/ratelimit.py`) keeps its state **in memory, per process**. With `--workers N` or several
replicas, each process has its own counters, so the effective limit becomes N times the configured value. Run
a single worker (the image does), or replace the limiter with a shared store such as Redis, or enforce limits
at the proxy. One process is enough for moderate traffic: scans run in a threadpool, and model inference is
serialised.

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

Before you publish this repository or the image, **verify the licence of every file in `tests/fixtures/`.**
`C.jpg` and `CA.jpg` come from [c2pa-rs](https://github.com/contentauth/c2pa-rs) (MIT/Apache-2.0). The
provenance of the other images and clips (`ai_*`, `real_*`, `test_clip.*`) has not been documented here.
Confirm it, or replace them with files you have rights to. `real_face_obama.jpg` shows a real person, so check
its licence and consider replacing it. `.dockerignore` keeps `tests/` out of the Docker image.
