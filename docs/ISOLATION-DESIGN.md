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

The parent sends a small versioned dictionary containing the file path, sniffed MIME type and whether decoded
frames are needed. The path always names the route-owned upload temporary file; workers do not accept HTTP
parameters directly.

The worker replies with a versioned dictionary of JSON-safe scalar/list/dict metadata plus RGB frame records:
`{width, height, mode: "RGB", data: bytes}`. Metadata field lengths retain the existing parser caps. Each frame
must be no larger than the configured decode-pixel budget, video frames retain the existing 1024-pixel long-side
cap, and the sum of RGB payloads must not exceed `BTF_SCAN_IPC_MB` (default 32 MiB). Both worker and parent
validate the response and reject wrong versions, dimensions, modes, frame counts, byte lengths or total payload
size. Exceptions are returned only as a small `{kind, message}` record; tracebacks and arbitrary objects never
cross IPC.

Non-JPEG images above `BTF_MAX_DECODE_PIXELS` are not fully decoded. Metadata and C2PA checks still run, then the
existing `AI detector could not analyze this file (...)` signal is added. JPEG uses libjpeg shrink-on-load at
1/2, 1/4 or 1/8 scale. The overall `BTF_MAX_IMAGE_PIXELS` dimension guard remains in force.

## Worker lifecycle and failure handling

Workers use Python's `spawn` multiprocessing context so they do not inherit parent threads, Torch state, file
descriptors or partially initialized native libraries. A process-local pool has `BTF_SCAN_CONCURRENCY` workers.
Each caller leases one worker, submits exactly one job and waits up to `BTF_SCAN_TIMEOUT_S` (default 30 seconds).
After `BTF_SCAN_WORKER_JOBS` jobs (default 50), the worker exits and is replaced before another lease.

If the deadline expires, the parent terminates the worker, escalates to a kill if necessary, joins it and creates
a replacement. The API returns status **504** with `{"error":"scan_timeout"}`. A 504 describes the actual
boundary: the web server accepted the request but its subordinate parsing service did not finish in time. A 503
is reserved for the existing concurrency queue exhaustion (`{"error":"busy"}`), which is retryable capacity
pressure rather than a particular scan exceeding its deadline. Worker exit, malformed IPC and setup failures
produce a clean 500 `{"error":"scan_failed"}` and also replace the worker. The upload is deleted in every case.

The pool has an explicit shutdown hook and an `atexit` fallback. Shutdown terminates and joins every child, so
normal application and test exits leave no zombies.

## Resource limits

At worker startup, supported Unix limits are applied:

- `RLIMIT_AS` from `BTF_SCAN_MAX_RSS_MB` (default 1024 MiB);
- `RLIMIT_CPU`, refreshed per job to the worker's consumed CPU plus the wall-clock scan budget;
- `RLIMIT_FSIZE=0`, because parser workers only read the parent-created temporary upload; and
- `RLIMIT_NOFILE` from `BTF_SCAN_MAX_NOFILE` (default 64).

`RLIMIT_AS` is effective on Linux but is ignored or unreliable on macOS. A worker watchdog therefore also polls
its own peak RSS and immediately exits if it exceeds the configured budget. Native code can temporarily prevent
the Python watchdog thread from running, so the deterministic pixel/frame/IPC budgets remain the primary memory
control on every platform, and the parent's wall deadline remains the final containment mechanism.

`BTF_SCAN_ISOLATION=off` keeps an in-process path for debugging and constrained test environments. It uses the
same parse-result contract and report assembly, so isolation does not change successful report shape or content.
Production defaults to `on`.

## Test-only fault injection

Tests may set `BTF_SCAN_TEST_HOOKS=1` in both parent and spawned workers and pass an internal hook directly to the
isolation API. Supported hooks sleep forever or call `os._exit`; no filename, MIME type, upload bytes, header,
query parameter or other HTTP input is mapped to a hook. This keeps the fault surface unreachable to clients.
