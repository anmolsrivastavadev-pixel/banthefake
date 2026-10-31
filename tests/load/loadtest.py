"""BanTheFake load test: spawns a real uvicorn server and measures scan / petition performance.

Uses only the stdlib and httpx. Not collected by pytest (no test_ prefix). Run from the repo root:

    python tests/load/loadtest.py latency            # per-fixture p50/p95 + cold first scan
    python tests/load/loadtest.py concurrency --slots 2 [--levels 1,2,4,8,16 --duration 60]
    python tests/load/loadtest.py petition [--concurrency 16 --duration 30]
    python tests/load/loadtest.py memory [--scans 300]

Every run prints JSON result lines (also appended to --out if given) including `uptime` load averages
before/after, so results from a shared machine can be judged. Environment knobs: BTF_LT_PYTHON (python used
to run uvicorn, default sys.executable), BTF_LT_PORT (default 8851); HF_HOME / HF_HUB_OFFLINE are inherited.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures"
PORT = int(os.environ.get("BTF_LT_PORT", "8851"))
BASE = f"http://127.0.0.1:{PORT}"
PYTHON = os.environ.get("BTF_LT_PYTHON", sys.executable)
MIXED = ("small_jpeg", "large_jpeg", "png", "c2pa_pixel", "mp4")


def _png_fixture() -> Path:
    """A 1280x720 PNG made from real_bridge.jpg (the fixtures have no PNG); cached in the temp dir."""
    out = Path(tempfile.gettempdir()) / "btf_loadtest_bridge.png"
    if not out.exists():
        src, dst = str(FIX / "real_bridge.jpg"), str(out)
        subprocess.run([PYTHON, "-c", f"from PIL import Image; Image.open({src!r}).save({dst!r})"], check=True)
    return out


def fixtures() -> dict[str, tuple[str, bytes, str]]:
    items = {
        "small_jpeg": (FIX / "real_cat_q70_640.jpg", "image/jpeg"),  # 640x360, 36 KB
        "large_jpeg": (FIX / "C.jpg", "image/jpeg"),  # 2048x1365, 130 KB
        "png": (_png_fixture(), "image/png"),  # 1280x720
        "c2pa_pixel": (FIX / "pixel10_verified.jpg", "image/jpeg"),  # 4080x3072, 2.9 MB, C2PA manifest
        "mp4": (FIX / "test_clip.mp4", "video/mp4"),  # 640x640 h264, 5 s
    }
    return {k: (p.name, p.read_bytes(), mime) for k, (p, mime) in items.items()}


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 4)


def loadavg() -> str:
    out = subprocess.run(["uptime"], capture_output=True, text=True).stdout
    return out.split("load average")[-1].strip(" :s\n") if "load average" in out else out.strip()


class Server:
    """uvicorn in a subprocess with an RSS sampler (ps every 0.5 s)."""

    def __init__(self, env: dict[str, str]) -> None:
        self.db_dir = tempfile.mkdtemp(prefix="btf-lt-")
        self.env = {
            **os.environ,
            "BTF_DB_PATH": str(Path(self.db_dir) / "lt.db"),
            "BTF_SCAN_RATE": "100000",
            "BTF_SIGN_RATE": "100000",
            "BTF_SIGN_ATTEMPT_RATE": "100000",
            **env,
        }
        self.proc: subprocess.Popen | None = None
        self.samples: list[tuple[float, int]] = []
        self._stop = threading.Event()
        self.log = Path(self.db_dir) / "server.log"

    def start(self) -> float:
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
            cwd=ROOT,
            env=self.env,
            stdout=self.log.open("w"),
            stderr=subprocess.STDOUT,
        )
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited; see {self.log}")
            try:
                if httpx.get(BASE + "/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        threading.Thread(target=self._sample, daemon=True).start()
        return time.perf_counter() - t0

    def wait_model(self, timeout: float = 300) -> float:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < timeout:
            if httpx.get(BASE + "/api/health", timeout=5).json().get("model") in ("loaded", "degraded"):
                return time.perf_counter() - t0
            time.sleep(0.25)
        raise RuntimeError("model did not load")

    def rss_mb(self) -> float:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.proc.pid)], capture_output=True, text=True).stdout
        return int(out.strip() or 0) / 1024

    def cpu_s(self) -> float:
        """Cumulative CPU time (user+sys, all threads) of the server process. Unlike wall-clock latency this
        barely moves when other processes compete for the CPU, so CPU-seconds per scan is the sizing number."""
        out = subprocess.run(["ps", "-o", "time=", "-p", str(self.proc.pid)], capture_output=True, text=True).stdout
        secs = 0.0
        for part in out.strip().split(":"):
            secs = secs * 60 + float(part or 0)
        return secs

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append((time.time(), int(self.rss_mb())))
            except Exception:
                pass
            self._stop.wait(0.5)

    def peak_rss(self, since: float = 0) -> int:
        return max((r for t, r in self.samples if t >= since), default=0)

    def stop(self) -> None:
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


async def scan_once(client: httpx.AsyncClient, fx: tuple[str, bytes, str]) -> tuple[int, float]:
    t0 = time.perf_counter()
    try:
        r = await client.post("/api/scan", files={"file": fx})
        status = r.status_code
    except httpx.HTTPError:
        status = -1
    return status, time.perf_counter() - t0


def client(n: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE, timeout=300, limits=httpx.Limits(max_connections=n + 4, max_keepalive_connections=n + 4)
    )


def emit(result: dict, out: str | None) -> None:
    line = json.dumps(result)
    print(line, flush=True)
    if out:
        with open(out, "a") as f:
            f.write(line + "\n")


# --- 1. single-request latency ---------------------------------------------------------------------------


async def run_latency(args) -> None:
    fx = fixtures()
    # Truly cold: fresh process, first scan sent as soon as /api/health answers (model still loading).
    with Server({}) as srv:
        before = loadavg()
        async with client(1) as c:
            st, dt = await scan_once(c, fx["small_jpeg"])
        emit({"test": "cold_start_first_scan", "fixture": "small_jpeg", "status": st, "s": round(dt, 3),
              "load_before": before, "load_after": loadavg()}, args.out)
    with Server({}) as srv:
        before = loadavg()
        model_load_s = srv.wait_model()
        async with client(1) as c:
            # First scan of each type after the background model load finished (kernels/caches not yet warm).
            cold = {}
            for name, f in fx.items():
                st, dt = await scan_once(c, f)
                cold[name] = {"status": st, "s": round(dt, 3)}
            emit({"test": "first_scan_after_model_load", "model_load_s": round(model_load_s, 2), "cold": cold,
                  "rss_mb": round(srv.rss_mb()), "load_before": before, "load_after": loadavg()}, args.out)
            for name, f in fx.items():
                if args.only and name not in args.only.split(","):
                    continue
                before = loadavg()
                for _ in range(args.warmup):
                    await scan_once(c, f)
                lat, statuses = [], {}
                cpu0 = srv.cpu_s()
                for _ in range(args.n):
                    st, dt = await scan_once(c, f)
                    statuses[st] = statuses.get(st, 0) + 1
                    lat.append(dt)
                emit({"test": "latency", "fixture": name, "bytes": len(f[1]), "n": args.n,
                      "statuses": {str(k): v for k, v in statuses.items()}, "p50": pct(lat, 50),
                      "p95": pct(lat, 95), "min": round(min(lat), 4), "mean": round(statistics.mean(lat), 4),
                      "cpu_s_per_scan": round((srv.cpu_s() - cpu0) / args.n, 3), "load_before": before, "load_after": loadavg()}, args.out)


# --- 2. throughput under concurrency ---------------------------------------------------------------------


async def run_level(n: int, duration: float, fx_list: list) -> dict:
    lat_ok: list[float] = []
    statuses: dict[int, int] = {}
    counter = 0
    deadline = time.perf_counter() + duration

    async def worker(c: httpx.AsyncClient) -> None:
        nonlocal counter
        while time.perf_counter() < deadline:
            f = fx_list[counter % len(fx_list)]
            counter += 1
            st, dt = await scan_once(c, f)
            statuses[st] = statuses.get(st, 0) + 1
            if st == 200:
                lat_ok.append(dt)

    t0 = time.perf_counter()
    async with client(n) as c:
        await asyncio.gather(*(worker(c) for _ in range(n)))
    elapsed = time.perf_counter() - t0
    total = sum(statuses.values())
    return {
        "concurrency": n,
        "elapsed_s": round(elapsed, 1),
        "requests": total,
        "statuses": {str(k): v for k, v in statuses.items()},
        "ok_per_min": round(len(lat_ok) / elapsed * 60, 1),
        "p50": pct(lat_ok, 50),
        "p95": pct(lat_ok, 95),
        "p99": pct(lat_ok, 99),
        "busy_503_rate": round(statuses.get(503, 0) / total, 4) if total else None,
    }


async def run_concurrency(args) -> None:
    fx = fixtures()
    fx_list = [fx[k] for k in MIXED]
    env = {"BTF_SCAN_CONCURRENCY": str(args.slots)}
    if args.wait is not None:
        env["BTF_SCAN_WAIT_S"] = str(args.wait)
    with Server(env) as srv:
        srv.wait_model()
        async with client(1) as c:
            for f in fx_list * 2:
                await scan_once(c, f)
        for n in [int(x) for x in args.levels.split(",")]:
            before = loadavg()
            since = time.time()
            cpu0 = srv.cpu_s()
            res = await run_level(n, args.duration, fx_list)
            ok = res["statuses"].get("200", 0)
            res["cpu_s_per_ok_scan"] = round((srv.cpu_s() - cpu0) / ok, 3) if ok else None
            res.update(test="concurrency", slots=args.slots, scan_wait_s=args.wait or 30.0,
                       peak_rss_mb=srv.peak_rss(since), load_before=before, load_after=loadavg())
            emit(res, args.out)


# --- 3. petition ------------------------------------------------------------------------------------------


async def run_petition(args) -> None:
    with Server({}) as srv:
        before = loadavg()
        lat: list[float] = []
        statuses: dict[int, int] = {}
        deadline = time.perf_counter() + args.duration
        run = uuid.uuid4().hex[:8]
        seq = 0

        async def worker(c: httpx.AsyncClient) -> None:
            nonlocal seq
            while time.perf_counter() < deadline:
                seq += 1
                body = {"name": f"Load Test {seq}", "email": f"lt{run}.{seq}@example.org",
                        "location": "Testville", "consent": True}
                t0 = time.perf_counter()
                try:
                    st = (await c.post("/api/petition", json=body)).status_code
                except httpx.HTTPError:
                    st = -1
                lat.append(time.perf_counter() - t0)
                statuses[st] = statuses.get(st, 0) + 1

        t0 = time.perf_counter()
        async with client(args.concurrency) as c:
            await asyncio.gather(*(worker(c) for _ in range(args.concurrency)))
            elapsed = time.perf_counter() - t0
            count = (await c.get("/api/petition/count")).json()["count"]
        peak = srv.peak_rss()
        srv.stop()
        log = srv.log.read_text()
        emit({
            "test": "petition", "concurrency": args.concurrency, "elapsed_s": round(elapsed, 1),
            "statuses": {str(k): v for k, v in statuses.items()},
            "signatures_per_s": round(statuses.get(201, 0) / elapsed, 1), "stored_count": count,
            "p50": pct(lat, 50), "p95": pct(lat, 95), "p99": pct(lat, 99),
            "sqlite_errors_in_log": sum(1 for ln in log.splitlines() if "sqlite3." in ln or "database is locked" in ln),
            "server_tracebacks": log.count("Traceback"), "peak_rss_mb": peak,
            "load_before": before, "load_after": loadavg(),
        }, args.out)


# --- 4. memory stability ----------------------------------------------------------------------------------


async def run_memory(args) -> None:
    fx = fixtures()
    fx_list = [fx[k] for k in MIXED]
    with Server({}) as srv:
        srv.wait_model()
        before = loadavg()
        rss_after_load = round(srv.rss_mb())
        trace, statuses = [], {}
        async with client(1) as c:
            for i in range(args.scans):
                st, _ = await scan_once(c, fx_list[i % len(fx_list)])
                statuses[st] = statuses.get(st, 0) + 1
                if (i + 1) % 25 == 0:
                    trace.append((i + 1, round(srv.rss_mb())))
        emit({"test": "memory", "scans": args.scans, "statuses": {str(k): v for k, v in statuses.items()},
              "rss_after_model_load_mb": rss_after_load, "rss_trace_mb": trace,
              "peak_rss_mb": srv.peak_rss(), "load_before": before, "load_after": loadavg()}, args.out)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", help="append JSON result lines to this file")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("latency")
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--warmup", type=int, default=3)
    s.add_argument("--only", help="comma-separated fixture names (default: all)")
    s = sub.add_parser("concurrency")
    s.add_argument("--slots", type=int, default=2)
    s.add_argument("--wait", type=float, default=None, help="BTF_SCAN_WAIT_S (server default 30)")
    s.add_argument("--levels", default="1,2,4,8,16")
    s.add_argument("--duration", type=float, default=60)
    s = sub.add_parser("petition")
    s.add_argument("--concurrency", type=int, default=16)
    s.add_argument("--duration", type=float, default=30)
    s = sub.add_parser("memory")
    s.add_argument("--scans", type=int, default=300)
    args = p.parse_args()
    runners = {"latency": run_latency, "concurrency": run_concurrency, "petition": run_petition, "memory": run_memory}
    asyncio.run(runners[args.cmd](args))


if __name__ == "__main__":
    main()
