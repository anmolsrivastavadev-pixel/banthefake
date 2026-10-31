"""Spawned-process boundary for all parsing of untrusted media.

The web process owns model inference.  Workers only parse metadata/C2PA and decode bounded RGB
frames, then return a small versioned record.  A worker is dedicated to one caller at a time, which
lets the caller kill exactly the process that exceeded its deadline.
"""

from __future__ import annotations

import atexit
import logging
import math
import multiprocessing as mp
import os
import pickle
import queue
import resource
import signal
import sys
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

log = logging.getLogger("banthefake.isolate")

PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RSS_MB = 1024
DEFAULT_MAX_NOFILE = 64
DEFAULT_MAX_JOBS = 50
DEFAULT_IPC_MB = 32
DEFAULT_MAX_DECODE_PIXELS = 25_000_000
C2PA_ENV = (
    "BTF_C2PA_TRUST", "BTF_C2PA_TRUST_ANCHORS", "BTF_C2PA_TRUST_ANCHORS_MODE",
)


class ScanTimeout(Exception):
    """A parser worker did not finish before the wall-clock deadline."""


class WorkerFailed(Exception):
    """A parser worker exited, rejected its job, or returned malformed IPC."""


class WorkerUnsupportedMedia(Exception):
    """The isolated parser safely rejected this media."""


def enabled() -> bool:
    return os.environ.get("BTF_SCAN_ISOLATION", "on").strip().lower() not in {"off", "0", "false", "no"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except ValueError:
        return default


def timeout_s() -> float:
    try:
        return max(0.05, float(os.environ.get("BTF_SCAN_TIMEOUT_S") or DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _ipc_limit() -> int:
    return _int_env("BTF_SCAN_IPC_MB", DEFAULT_IPC_MB) * 1024 * 1024


def _test_hook() -> str:
    if os.environ.get("BTF_SCAN_TEST_HOOKS") == "1":
        return os.environ.get("BTF_SCAN_TEST_HOOK", "")
    return ""


@dataclass
class _Worker:
    process: mp.Process
    connection: Connection
    jobs: int = 0


class _Pool:
    def __init__(self, size: int) -> None:
        self.size = size
        self._ctx = mp.get_context("spawn")
        self._available: queue.Queue[_Worker] = queue.Queue(maxsize=size)
        self._all: list[_Worker] = []
        self._closed = False
        for _ in range(size):
            self._available.put(self._spawn())

    def _spawn(self) -> _Worker:
        parent, child = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(target=_worker_main, args=(child,), name="btf-media-parser", daemon=True)
        proc.start()
        child.close()
        worker = _Worker(proc, parent)
        self._all.append(worker)
        return worker

    def _discard(self, worker: _Worker) -> None:
        if worker in self._all:
            self._all.remove(worker)
        try:
            worker.connection.close()
        except OSError:
            pass
        if worker.process.is_alive():
            worker.process.terminate()
            worker.process.join(0.5)
        if worker.process.is_alive():
            worker.process.kill()
            worker.process.join(1.0)
        else:
            worker.process.join(0.1)

    def _replace(self, worker: _Worker) -> _Worker:
        self._discard(worker)
        if self._closed:
            raise WorkerFailed("parser pool is closed")
        return self._spawn()

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        deadline = timeout_s()
        try:
            worker = self._available.get(timeout=deadline)
        except queue.Empty as exc:
            raise ScanTimeout("no parser worker became available") from exc

        reusable = True
        try:
            if not worker.process.is_alive():
                worker = self._replace(worker)
            payload = pickle.dumps(request, protocol=5)
            if len(payload) > 64 * 1024:
                raise WorkerFailed("parser request exceeds IPC limit")
            worker.connection.send_bytes(payload)
            if not worker.connection.poll(deadline):
                reusable = False
                self._discard(worker)
                raise ScanTimeout(f"parser exceeded {deadline:g} seconds")
            try:
                raw = worker.connection.recv_bytes(maxlength=_ipc_limit() + 1)
            except (EOFError, OSError) as exc:
                reusable = False
                self._discard(worker)
                raise WorkerFailed("parser worker exited or exceeded the IPC limit") from exc
            worker.jobs += 1
            try:
                response = pickle.loads(raw)
            except Exception as exc:
                reusable = False
                self._discard(worker)
                raise WorkerFailed("parser returned unreadable IPC") from exc
            if not isinstance(response, dict) or response.get("version") != PROTOCOL_VERSION:
                reusable = False
                self._discard(worker)
                raise WorkerFailed("parser returned an invalid protocol record")
            if response.get("ok") is not True:
                error = response.get("error") if isinstance(response.get("error"), dict) else {}
                kind, message = error.get("kind"), str(error.get("message") or "scan failed")[:500]
                if kind == "unsupported_media":
                    raise WorkerUnsupportedMedia(message)
                reusable = False
                self._discard(worker)
                raise WorkerFailed(message)
            result = response.get("result")
            try:
                _validate_result(result, _ipc_limit())
            except WorkerFailed:
                reusable = False
                self._discard(worker)
                raise
            if worker.jobs >= _int_env("BTF_SCAN_WORKER_JOBS", DEFAULT_MAX_JOBS):
                reusable = False
                self._discard(worker)
            return result
        except (BrokenPipeError, EOFError, OSError) as exc:
            if reusable:
                reusable = False
                self._discard(worker)
            raise WorkerFailed("parser worker communication failed") from exc
        finally:
            if not self._closed:
                if reusable and worker.process.is_alive():
                    self._available.put(worker)
                elif not reusable:
                    self._available.put(self._spawn())

    def close(self) -> None:
        self._closed = True
        for worker in list(self._all):
            self._discard(worker)
        while True:
            try:
                self._available.get_nowait()
            except queue.Empty:
                break

    def pids(self) -> list[int]:
        return [w.process.pid for w in self._all if w.process.pid and w.process.is_alive()]


_pool: _Pool | None = None
_pool_size = _int_env("BTF_SCAN_CONCURRENCY", 2)
_pool_lock = threading.Lock()


def configure(concurrency: int) -> None:
    """Set the lazy pool size. Reconfigure an idle pool on the next scan."""
    global _pool_size, _pool
    size = max(1, int(concurrency))
    with _pool_lock:
        _pool_size = size
        if _pool is not None and _pool.size != size:
            _pool.close()
            _pool = None


def _get_pool() -> _Pool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _Pool(_pool_size)
        return _pool


def parse(path: str | Path, filename: str, content_type: str, want_frames: bool) -> dict[str, Any]:
    """Parse one upload in an isolated process and return the bounded parse record."""
    request = {
        "version": PROTOCOL_VERSION,
        "path": os.fspath(path),
        "filename": filename[:255],
        "content_type": content_type[:80],
        "want_frames": bool(want_frames),
        "test_hook": _test_hook(),
        "environment": {name: os.environ.get(name) for name in C2PA_ENV},
    }
    return _get_pool().run(request)


def shutdown() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def active_worker_pids() -> list[int]:
    with _pool_lock:
        return _pool.pids() if _pool is not None else []


atexit.register(shutdown)


def _worker_main(connection: Connection) -> None:
    # Ignore Ctrl-C intended for the web process; the parent owns worker lifecycle.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _apply_startup_limits()
    _start_rss_watchdog()
    jobs = 0
    max_jobs = _int_env("BTF_SCAN_WORKER_JOBS", DEFAULT_MAX_JOBS)
    try:
        while jobs < max_jobs:
            try:
                raw = connection.recv_bytes(maxlength=64 * 1024)
            except (EOFError, OSError):
                break
            try:
                request = pickle.loads(raw)
                _validate_request(request)
                _apply_job_environment(request["environment"])
                _refresh_cpu_limit(timeout_s())
                hook = request.get("test_hook")
                if hook == "hang" and os.environ.get("BTF_SCAN_TEST_HOOKS") == "1":
                    while True:
                        time.sleep(60)
                if hook == "crash" and os.environ.get("BTF_SCAN_TEST_HOOKS") == "1":
                    os._exit(86)
                from app import scan

                result = scan._parse_hostile(
                    request["path"], request["content_type"], request["want_frames"]
                )
                _validate_result(result, _ipc_limit())
                response = {"version": PROTOCOL_VERSION, "ok": True, "result": result}
            except BaseException as exc:
                kind = "unsupported_media" if type(exc).__name__ == "UnsupportedMedia" else "scan_failed"
                response = {
                    "version": PROTOCOL_VERSION,
                    "ok": False,
                    "error": {"kind": kind, "message": str(exc)[:500] or type(exc).__name__},
                }
            encoded = pickle.dumps(response, protocol=5)
            if len(encoded) > _ipc_limit():
                encoded = pickle.dumps(
                    {"version": PROTOCOL_VERSION, "ok": False,
                     "error": {"kind": "scan_failed", "message": "parser result exceeds IPC limit"}},
                    protocol=5,
                )
            try:
                connection.send_bytes(encoded)
            except (BrokenPipeError, EOFError, OSError):
                break
            jobs += 1
    finally:
        connection.close()


def _validate_request(request: object) -> None:
    if not isinstance(request, dict) or request.get("version") != PROTOCOL_VERSION:
        raise ValueError("invalid parser request")
    if not isinstance(request.get("path"), str) or not isinstance(request.get("content_type"), str):
        raise ValueError("invalid parser request fields")
    if not isinstance(request.get("want_frames"), bool):
        raise ValueError("invalid parser frame flag")
    environment = request.get("environment")
    if not isinstance(environment, dict) or set(environment) != set(C2PA_ENV):
        raise ValueError("invalid parser environment")
    if any(value is not None and not isinstance(value, str) for value in environment.values()):
        raise ValueError("invalid parser environment value")


def _apply_job_environment(environment: dict[str, str | None]) -> None:
    for name in C2PA_ENV:
        value = environment[name]
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _validate_result(result: object, limit: int) -> None:
    if not isinstance(result, dict) or result.get("version") != PROTOCOL_VERSION:
        raise WorkerFailed("invalid parser result")
    frames = result.get("frames")
    if not isinstance(frames, list) or len(frames) > 8:
        raise WorkerFailed("invalid parser frame list")
    total = 0
    for frame in frames:
        if not isinstance(frame, dict) or frame.get("mode") != "RGB":
            raise WorkerFailed("invalid parser frame")
        w, h, data = frame.get("width"), frame.get("height"), frame.get("data")
        if not isinstance(w, int) or not isinstance(h, int) or w < 1 or h < 1:
            raise WorkerFailed("invalid parser frame dimensions")
        if w * h > _int_env("BTF_MAX_DECODE_PIXELS", DEFAULT_MAX_DECODE_PIXELS):
            raise WorkerFailed("parser frame exceeds the pixel budget")
        if not isinstance(data, bytes) or len(data) != w * h * 3:
            raise WorkerFailed("invalid parser RGB payload")
        total += len(data)
        if total > limit:
            raise WorkerFailed("parser RGB payload exceeds IPC limit")


def _apply_startup_limits() -> None:
    rss_bytes = _int_env("BTF_SCAN_MAX_RSS_MB", DEFAULT_MAX_RSS_MB) * 1024 * 1024
    _set_limit("RLIMIT_AS", rss_bytes)
    _set_limit("RLIMIT_FSIZE", 0)
    _set_limit("RLIMIT_NOFILE", _int_env("BTF_SCAN_MAX_NOFILE", DEFAULT_MAX_NOFILE, 16))


def _set_limit(name: str, soft: int) -> None:
    limit = getattr(resource, name, None)
    if limit is None:
        return
    try:
        _, hard = resource.getrlimit(limit)
        value = soft if hard == resource.RLIM_INFINITY else min(soft, hard)
        resource.setrlimit(limit, (value, hard))
    except (OSError, ValueError):
        # macOS exposes RLIMIT_AS but does not reliably implement it. Pixel/IPC budgets, the RSS
        # watchdog and wall deadline remain active there.
        message = "could not apply %s to parser worker"
        (log.info if name == "RLIMIT_AS" and sys.platform == "darwin" else log.warning)(message, name)


def _refresh_cpu_limit(seconds: float) -> None:
    limit = getattr(resource, "RLIMIT_CPU", None)
    if limit is None:
        return
    used = resource.getrusage(resource.RUSAGE_SELF)
    soft = math.ceil(used.ru_utime + used.ru_stime + seconds) + 1
    try:
        _, hard = resource.getrlimit(limit)
        value = soft if hard == resource.RLIM_INFINITY else min(soft, hard)
        resource.setrlimit(limit, (value, hard))
    except (OSError, ValueError):
        log.warning("could not refresh RLIMIT_CPU in parser worker")


def _rss_bytes() -> int:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss if sys.platform == "darwin" else rss * 1024)


def _start_rss_watchdog() -> None:
    maximum = _int_env("BTF_SCAN_MAX_RSS_MB", DEFAULT_MAX_RSS_MB) * 1024 * 1024

    def watch() -> None:
        while True:
            if _rss_bytes() > maximum:
                os._exit(87)
            time.sleep(0.1)

    threading.Thread(target=watch, name="btf-parser-rss", daemon=True).start()
