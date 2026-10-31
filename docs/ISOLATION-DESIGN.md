# Hostile-media process isolation

## Scope and trust boundary

Uploaded bytes are untrusted. Every operation that interprets those bytes runs in a spawned worker process:

- raw marker-window reads and structured marker scanning;
- Pillow format probing, EXIF, XMP, PNG chunk/text parsing, decompression and image decoding;
- PyAV container opening, metadata reads, seeking, codec decode and frame sampling;
- C2PA `Reader` construction, manifest parsing, validation and summary extraction; and
- the HEIC/HEIF capability probe that previously called Pillow in the web process.

The parent process still streams the upload to a temporary file, checks its first 64 bytes with the fixed-size
magic-byte sniffer, owns request rate/concurrency limits, assembles the existing report/verdict, runs detector
inference and deletes the upload in the route's `finally` block. The parent never asks Pillow, PyAV or C2PA to
open an uploaded file.

## Model placement

Both detector models remain in the parent and are loaded once. Parser workers return decoded, oriented,
downsampled RGB frames. The parent reconstructs Pillow `RGB` images from those already-decoded bytes and scores
them with the existing model API.

This is preferable to loading Torch in each parser worker: the shipped models use roughly 1.5 GiB resident and
can peak near 2.5 GiB during an eight-frame batch. Loading them in every worker would multiply memory use by
`BTF_SCAN_CONCURRENCY`, lengthen worker startup and throw away warm model state whenever a parser is killed or
recycled. A separate long-lived inference worker would serialize all model work behind another IPC hop and would
add a new single point of failure without improving parser containment. Keeping inference in the bounded parent
scan slot preserves current latency and concurrency behaviour.

## IPC contract and bounds

The parent sends a small, strictly validated UTF-8 JSON object containing the file path, sniffed MIME type,
remaining time budget and whether decoded frames are needed. The path always names the route-owned upload
temporary file; workers do not accept HTTP parameters directly.

The worker reply is passive data: a four-byte big-endian JSON-header length, the UTF-8 JSON header, then the raw
RGB blocks declared by that header. The parent never unpickles worker bytes. The header is capped at 256 KiB;
duplicate JSON keys, unknown or missing fields, wrong types, non-finite/out-of-range numbers, oversized strings
and lists, inconsistent dimensions/timestamps, and malformed credential or marker records are rejected. Every
frame length and the total payload length are checked before any RGB block is sliced or reconstructed. Missing
or trailing bytes are protocol errors. Each frame must be no larger than the configured decode-pixel budget,
video frames retain the existing 1024-pixel long-side cap, and the sum of RGB payloads must not exceed
`BTF_SCAN_IPC_MB` (default 32 MiB). Exceptions are returned only as a small `{kind, message}` JSON record;
tracebacks and arbitrary objects never cross IPC.

Non-JPEG images above `BTF_MAX_DECODE_PIXELS` are not fully decoded. Metadata and C2PA checks still run, then the
existing `AI detector could not analyze this file (...)` signal is added. JPEG uses libjpeg shrink-on-load at
1/2, 1/4 or 1/8 scale. The overall `BTF_MAX_IMAGE_PIXELS` dimension guard remains in force.

## Worker lifecycle and failure handling

Workers use Python's `spawn` multiprocessing context so they do not inherit parent threads, Torch state, file
descriptors or partially initialized native libraries. A process-local pool has `BTF_SCAN_CONCURRENCY` workers.
The parent streams the upload under the separate `BTF_UPLOAD_TIMEOUT_S` limit (default 60 seconds), then waits
up to `BTF_SCAN_WAIT_S` (default 30 seconds) for an API concurrency slot. Upload timeout returns 408
`{"error":"upload_timeout"}`; queue timeout returns 503 `{"error":"busy"}` with `Retry-After`. Once a slot is
acquired, the request computes one absolute monotonic deadline from `BTF_SCAN_TIMEOUT_S` (default 30 seconds).
That full budget covers parser-pool acquisition, worker parsing, IPC validation, RGB reconstruction and model
inference. Each caller leases one worker for one parser job. After `BTF_SCAN_WORKER_JOBS` jobs (default 50), the
worker is replaced.

If the deadline expires, the parent terminates the worker, escalates to a kill if necessary, joins it and creates
a replacement. The API returns status **504** with `{"error":"scan_timeout"}`. A 504 describes the actual
boundary: the web server accepted the request but its subordinate parsing service did not finish in time. A 503
is reserved for concurrency queue exhaustion (`{"error":"busy"}`), which is retryable capacity pressure rather
than a particular scan exceeding its deadline. Worker exit, malformed IPC and setup failures produce a clean
500 `{"error":"scan_failed"}` and also replace the worker. The upload is deleted in every case, including upload
timeout.

Lease completion, retirement and shutdown change pool state under one lock. A replacement re-checks the closed
state after process creation and immediately terminates a child spawned during shutdown. A failed replacement
temporarily degrades capacity and is retried on the next acquisition. Cancellation maps the request to its exact
leased worker: a disconnected/cancelled request terminates that process immediately, restores capacity, releases
the API slot and deletes the upload. The pool also has an explicit shutdown hook and an `atexit` fallback.

Before model inference, the parent requires at least `BTF_SCAN_INFER_RESERVE_S` (default 3 seconds) to remain.
If less remains, it skips inference and emits the existing “AI detector could not analyze” information signal
with a time-budget reason. Video frames are scored in small batches and the parent stops between batches when
the deadline expires. A model call already executing in the web process cannot be interrupted safely; a strict
hard bound for that individual call would require moving inference into a separately killable process.

## Resource limits

At worker startup, supported Unix limits are applied:

- `RLIMIT_AS` from `BTF_SCAN_MAX_RSS_MB` (default 1024 MiB);
- `RLIMIT_CPU`, refreshed per job to the worker's consumed CPU plus the wall-clock scan budget;
- `RLIMIT_FSIZE` from `BTF_SCAN_TEMP_MB` (default 64 MiB); and
- `RLIMIT_NOFILE` from `BTF_SCAN_MAX_NOFILE` (default 64).

Each worker creates a mode-0700 private temporary directory, points `TMPDIR`, `TMP` and `TEMP` at it, checks its
aggregate size before and after jobs, and has a watchdog terminate the worker if the directory exceeds the same
quota. The directory is removed when the worker exits. A nonzero file-size limit is required because PyAV and
C2PA/native dependencies may legitimately need temporary files.

`RLIMIT_AS` is effective on Linux but is ignored or unreliable on macOS. The worker watchdog also polls its own
peak RSS and exits if it exceeds the configured budget. Native code can temporarily prevent the Python watchdog
thread from running, so deterministic pixel/frame/IPC budgets remain the primary memory control on every
platform, and the parent's wall deadline remains the final parser containment mechanism.

`BTF_SCAN_ISOLATION=off` keeps an in-process path for debugging and constrained test environments. It uses the
same parse-result contract and report assembly, so isolation does not change successful report shape or content.
Production defaults to `on`.

## Test-only fault injection

Tests may set `BTF_SCAN_TEST_HOOKS=1` in both parent and spawned workers and pass an internal hook directly to the
isolation API. Supported hooks sleep forever or call `os._exit`; no filename, MIME type, upload bytes, header,
query parameter or other HTTP input is mapped to a hook. This keeps the fault surface unreachable to clients.
