# Performance and sizing

Measured 2026-10-31 on commit `3a9e3de` with `tests/load/loadtest.py`. Raw JSON lines:
`tests/load/results-2026-10-31.jsonl`.

**Treat every number here as indicative.** The machine was shared with a GPU training job, other agents'
pytest runs, Chrome, and another uvicorn server. Load averages were between 6 and 92 on 10 cores during the
runs, and swung within single runs. Wall-clock latencies under concurrency are dominated by that noise. The
most robust numbers are the **best-case latencies** and the **CPU seconds per scan**, measured from the
server process's own CPU time. They are much less sensitive to other processes, and they drive the sizing
guidance at the end.

## Environment

| | |
|---|---|
| Machine | Apple M5, 10 cores (4 performance + 6 efficiency), 16 GB RAM, macOS 26.6.2 |
| Software | Python 3.12.14, torch 2.15.0 (CPU, default 4 intra-op threads), transformers 5.22.1, FastAPI 0.162.0, uvicorn 0.54.0 |
| Server | `uvicorn app.main:app --port 8851` with one worker, started from the repo by the script. `HF_HUB_OFFLINE=1`, local HF cache, both detectors in agreement mode (default). The scan, sign and attempt rate limits were raised to 100000 so they don't affect the results. |
| Client | The same machine, httpx async. One TCP connection per virtual user. |
| Load averages | Recorded before and after every measurement (`uptime`). Quoted next to each table. |

Fixtures (from `tests/fixtures`, plus one PNG):

| name | file | size | pixels |
|---|---|---|---|
| small_jpeg | real_cat_q70_640.jpg | 36 KB | 640x360 |
| large_jpeg | C.jpg | 130 KB | 2048x1365 |
| png | real_bridge.jpg re-encoded as PNG by the script | 1.6 MB | 1280x720 |
| c2pa_pixel | pixel10_verified.jpg (C2PA, Pixel 10) | 2.9 MB | 4080x3072 |
| mp4 | test_clip.mp4 (h264, 5 s) | 411 KB | 640x640 |

The mixed workload cycles through the five fixtures round-robin: 4 images and 1 video.

## Method

```
HF_HOME=.../hf_cache HF_HUB_OFFLINE=1 python tests/load/loadtest.py --out results.jsonl latency
python tests/load/loadtest.py concurrency --slots 2      # BTF_SCAN_CONCURRENCY=2, levels 1,2,4,8,16 x 60 s
python tests/load/loadtest.py concurrency --slots 4
python tests/load/loadtest.py petition                   # 16 users x 30 s, unique emails
python tests/load/loadtest.py memory                     # 300 sequential mixed scans
BTF_MODEL_THREADS=1 python tests/load/loadtest.py ...    # 1 torch thread per scan (small-box emulation)
```

- Each subcommand starts a fresh server on port 8851 with a temporary SQLite database. It waits for
  `/api/health`, and for `model == "loaded"` except in the cold-start test. A background thread samples
  the server's RSS with `ps` every 0.5 s. The server is stopped at the end.
- Latency is client-side wall time for the whole `POST /api/scan` (upload, sniff, queue, scan, response).
- Concurrency runs N closed-loop virtual users for 60 s each. In-flight requests finish after the deadline,
  so the elapsed time can exceed 60 s. Throughput counts only `200` responses. Percentiles cover only `200`s.
  The 503 rate is `busy` responses divided by all responses. 503 is returned when a request waits longer than
  `BTF_SCAN_WAIT_S` (30 s) for a scan slot.
- `cpu_s` per scan is the server's cumulative CPU time delta (`ps -o time`, all threads) divided by the
  number of scans.

## 1. Single-request latency (sequential, after warm-up)

The latency subcommand ran twice with the default 4 torch threads (runs A and B), and once with
`BTF_MODEL_THREADS=1`. Each run did 20 scans per fixture (10 for the 1-thread run) after 3 (2) warm-up scans.

| fixture | A p50 / p95 (s) | B p50 / p95 (s) | B min (s) | B CPU s/scan | 1 thread p50 / p95 (s) | 1 thread CPU s/scan |
|---|---|---|---|---|---|---|
| small_jpeg | 0.43 / 0.44 | 0.39 / 1.56 | 0.33 | 1.12 | 2.34 / 4.54 | 1.83 |
| large_jpeg | 0.36 / 0.43 | 0.41 / 1.49 | 0.36 | 1.22 | 1.42 / 1.88 | 1.53 |
| png | 0.38 / 0.46 | 1.06 / 1.51 | 0.38 | 1.48 | 2.16 / 3.55 | 2.04 |
| c2pa_pixel | 2.83 / 5.42 | 0.52 / 2.45 | 0.51 | 1.18 | 2.09 / 3.23 | 2.09 |
| mp4 (8 frames x 2 models) | 2.45 / 18.9 | 3.00 / 10.6 | 2.00 | 7.68 | 3.80 / 5.81 | 4.58 |

Load averages: A 6.9 to 15 (it reached 15 during the mp4 rows), B 6.6 to 9.8, 1 thread 6.0 to 9.1.

- On an idle core an image scan takes about **0.35 to 0.5 s**. Image size barely matters, because every
  image is downscaled before inference. The 12-megapixel C2PA file costs about the same as the others.
  Its slow run A figure came when load rose to 8 and then 15.
- A video samples 8 frames, and each frame runs through both models. That makes it about **2 to 3 s at best**
  and about **4 to 8 times the CPU of an image**.
- Wide p95s and gaps between runs come from contention. With 1 thread, macOS often puts the work on an
  efficiency core, which explains the 0.57 s minimum against the 2.3 s p50 for small_jpeg.

### Cold start

| measurement | default threads | 1 thread |
|---|---|---|
| Fresh process, first scan sent as soon as `/api/health` answers (model still loading) | 14.0 s (A), 10.2 s (B) | 4.5 s |
| Model load time after health is up (`model` turns `loaded`) | 10.2 s (A), 4.1 s (B) | 2.8 s |
| First scan of each type after the model loaded | images 0.45 to 1.4 s, mp4 2.8 to 9.0 s | images 0.6 to 0.8 s, mp4 4.1 s |

The first scan on a new process waits for the background model load. Point readiness probes at
`/api/health` and wait for `"model":"loaded"` before routing traffic.

## 2. Throughput under concurrency (mixed fixtures, 60 s per level)

### `BTF_SCAN_CONCURRENCY=2` (default), default threads, run B

Load averages: 6.9 at the start. Then 15 to 32 during the runs.

| users | scans/min | p50 (s) | p95 (s) | p99 (s) | 503 rate | peak RSS (MB) | CPU s/scan |
|---|---|---|---|---|---|---|---|
| 1 | 23.6 | 1.35 | 12.9 | 15.2 | 0% | 2415 | 3.46 |
| 2 | 19.1 | 2.96 | 13.3 | 13.6 | 0% | 2244 | 5.66 |
| 4 | 50.7 | 2.81 | 6.08 | 32.5 | 2.9% | 2289 | 1.98 |
| 8 | 16.1 | 23.7 | 34.7 | 36.0 | 4.0% | 2181 | 6.08 |
| 16 | 52.3 | 9.62 | 27.2 | 41.4 | 11.3% | 2281 | 2.24 |

Run A had the same configuration under heavier load (load 13, rising to 46). It measured 32, 8.3, 33,
9.3 and 36 scans/min, with 503 rates of 0%, 0%, 2.1%, 32% and 17%.

### `BTF_SCAN_CONCURRENCY=4`, default threads

Load averages: 15, falling to 7.5, then 11.6 at the end.

| users | scans/min | p50 (s) | p95 (s) | p99 (s) | 503 rate | peak RSS (MB) | CPU s/scan |
|---|---|---|---|---|---|---|---|
| 1 | 51.7 | 0.45 | 2.76 | 8.16 | 0% | 2226 | 1.91 |
| 2 | 17.7 | 4.55 | 12.8 | 13.1 | 0% | 1913 | 5.90 |
| 4 | 32.4 | 3.64 | 17.3 | 18.5 | 0% | 2336 | 3.65 |
| 8 | 46.7 | 4.98 | 30.8 | 33.2 | 0% | 2420 | 2.68 |
| 16 | 22.5 | 26.7 | 49.0 | 49.7 | 30% | 2348 | 4.79 |

### `BTF_SCAN_CONCURRENCY=2`, `BTF_MODEL_THREADS=1` (small-box emulation)

Load averages: 11.5, then 7.7 to 27.6.

| users | scans/min | p50 (s) | p95 (s) | p99 (s) | 503 rate | peak RSS (MB) | CPU s/scan |
|---|---|---|---|---|---|---|---|
| 1 | 23.4 | 1.69 | 8.78 | 13.8 | 0% | 2286 | 2.60 |
| 2 | 25.5 | 2.17 | 21.6 | 28.5 | 0% | 2208 | 2.47 |
| 4 | 9.4 | 29.7 | 35.4 | 36.2 | 7.7% | 1781 | 6.15 |
| 8 | 26.8 | 9.29 | 15.5 | 50.0 | 12.8% | 2229 | 2.06 |
| 16 | 10.8 | 32.5 | 54.3 | 62.7 | 59% | 2014 | 5.87 |

How to read these tables:

- Throughput does not change monotonically with the number of users, and adjacent levels differ by 3 to
  5 times. That is the shared machine, not the app. In the bad windows, CPU per scan also jumps to about 6 s.
  The same work took more CPU time because of thread contention and efficiency-core scheduling. The
  **best windows show the real ceiling on this machine: about 50 mixed scans/min, at about 2 CPU s per mixed
  scan**.
- The semaphore works as designed. Requests queue, and once the queue wait passes 30 s, clients get a fast
  `503 busy` with `Retry-After: 30` instead of piling up. With 2 slots, 503s start at 4 to 8 users. With 4
  slots, they stay at 0% up to 8 users on this machine.
- Peak RSS stayed between **1.8 and 2.4 GB** at every level and slot count. Most of it is the two loaded
  models. 4 slots added no measurable memory with these fixtures. Large uploads, up to 50 MB and 8K video
  frames, would add more per slot.
- p95 for the mix is set by the video scans. Images alone stay under 2 s until the queue builds.

## 3. Petition endpoint

16 users for 30 s, every request with a unique email. Load average 22.5 at the start, 18.8 at the end.

| signatures/s | requests | statuses | p50 | p95 | p99 | SQLite errors | server tracebacks | stored rows |
|---|---|---|---|---|---|---|---|---|
| **80.8** | 2432 | 201 x 2432 | 84 ms | 798 ms | 1.45 s | 0 | 0 | 2432 (matches) |

Every 201 was stored, and there was no `database is locked` error. Each insert opens a new connection in the
threadpool, uses SQLite's default rollback journal, and has a 10 s busy timeout. Writes serialize on the file
lock, which explains the p95 of about 0.8 s at 16 concurrent writers. 80 signatures/s is about 290k/hour,
far above any plausible petition traffic. The 5/hour per-IP limit is the real constraint.

## 4. Memory stability (300 sequential mixed scans)

All 300 returned 200. Load average 16.9 at the start and 92.8 at the end. Another workload ran
heavily at the same time.

| after scan | 25 | 50 | 75 | 100 | 125 | 150 | 175 | 200 | 225 | 250 | 275 | 300 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RSS (MB) | 1942 | 1441 | 1779 | 1118 | 2205 | 1223 | 1637 | 1832 | 1763 | 1668 | 1950 | 1081 |

Peak RSS was 2297 MB. RSS right after the model load was 347 MB, because the safetensors weights are
memory-mapped and page in lazily.

**No leak is visible.** RSS moves between about 1.1 and 2.3 GB with no upward trend, and the last sample is
the lowest. The swings come from macOS reclaiming and compressing clean, memory-mapped model pages under
pressure from other processes. On Linux with no other pressure, expect a steadier figure near the peak,
about 2.2 to 2.4 GB. This test should be repeated on the deployment target (Linux, cgroup memory
accounting) before trusting it fully.

## Sizing guidance

Cost model, from the cleanest measurements on an M5 core:

- An image scan takes **about 1.2 to 2 CPU seconds**, or 0.35 to 0.5 s wall time on an idle machine.
- A video scan (8 frames, 2 models) takes **about 4.5 to 7.7 CPU seconds**, or 2 to 4 s wall time.
- The mixed workload (4 images to 1 video) averages **about 2 to 2.5 CPU seconds per scan**.
- Memory is **about 2.3 GB resident** with both models loaded, almost independent of concurrency at 2 to 4
  slots with typical files.

Typical cloud vCPUs (one hyperthread of a server x86 core) run torch inference somewhere around 1.5 to 2
times slower than an M5 performance core. These estimates assume that:

| box | settings | estimated sustained throughput | expected latency |
|---|---|---|---|
| **2 vCPU / 4 GB** | `BTF_SCAN_CONCURRENCY=2`, `BTF_MODEL_THREADS=1` | **about 15 to 25 mixed scans/min** (image-only about 30 to 45/min) | p95 about 3 s for images and 8 to 12 s for videos at 2 concurrent users. 503s once more than about 4 to 6 users scan at once. |
| 4 vCPU / 8 GB | `BTF_SCAN_CONCURRENCY=2`, `BTF_MODEL_THREADS=2` (or 4 slots with 1 thread) | about 35 to 50 mixed scans/min | p95 about 2 s for images and 5 to 8 s for videos at 4 users |
| 8 vCPU / 8 GB | `BTF_SCAN_CONCURRENCY=4`, `BTF_MODEL_THREADS=2` | about 70 to 100 mixed scans/min | similar to the row above, at 8 users |

Recommendations:

1. **Set `BTF_MODEL_THREADS` so that slots x threads is at most the vCPU count.** The torch default here was 4
   threads. Two concurrent scans then run 8 threads, and on a 2-vCPU box they would fight each other.
   Oversubscribed, contended runs spent 5 to 6 CPU seconds per mixed scan instead of about 2.
2. **4 GB is enough for 2 slots** (about 2.3 GB resident, plus the OS and Python), but leaves little headroom.
   Don't raise slots above 2 on 4 GB until large-upload memory has been measured.
3. Keep the 30 s queue wait. It turns overload into quick `503 busy` responses rather than unbounded latency
   or memory growth. The default per-IP limit of 10 scans/min (`BTF_SCAN_RATE`) keeps a single client well
   below the capacity of a 2-vCPU box.
4. Gate readiness on `model == "loaded"`. A cold process needs 3 to 10 s to load the models, and a scan sent
   earlier waits for the load.
5. The petition endpoint needs no sizing attention. SQLite handled about 80 signatures/s with 16 concurrent
   writers and no errors.

Before committing to a box size, rerun `tests/load/loadtest.py` on the actual target instance with nothing
else running. These numbers come from a heavily shared laptop.
