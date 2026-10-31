"""Spawned-process boundary for all parsing of untrusted media.

The web process owns model inference.  Workers only parse metadata/C2PA and decode bounded RGB
frames, then return a small versioned record.  A worker is dedicated to one caller at a time, which
lets the caller kill exactly the process that exceeded its deadline.
"""

from __future__ import annotations

import atexit
import json
import logging
import math
import multiprocessing as mp
import os
import queue
import resource
import shutil
import signal
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

log = logging.getLogger("banthefake.isolate")

PROTOCOL_VERSION = 1
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_INFER_RESERVE_S = 3.0
DEFAULT_MAX_RSS_MB = 1024
DEFAULT_MAX_NOFILE = 64
DEFAULT_MAX_JOBS = 50
DEFAULT_IPC_MB = 32
DEFAULT_TEMP_MB = 64
DEFAULT_MAX_DECODE_PIXELS = 25_000_000
HEADER_MAX = 256 * 1024
REQUEST_MAX = 64 * 1024
MAX_STRING = 4096
MAX_HITS = 128
MAX_NOTES = 32
MAX_CRED_ITEMS = 128
MAX_INGREDIENTS = 64
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


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name) or default)
        return max(minimum, value) if math.isfinite(value) else default
    except ValueError:
        return default


def timeout_s() -> float:
    return _float_env("BTF_SCAN_TIMEOUT_S", DEFAULT_TIMEOUT_S, 0.05)


def infer_reserve_s() -> float:
    return _float_env("BTF_SCAN_INFER_RESERVE_S", DEFAULT_INFER_RESERVE_S, 0.0)


def deadline_after_timeout() -> float:
    return time.monotonic() + timeout_s()


def remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def check_deadline(deadline: float) -> None:
    if remaining(deadline) <= 0:
        raise ScanTimeout("scan time budget exhausted")


def _ipc_limit() -> int:
    return _int_env("BTF_SCAN_IPC_MB", DEFAULT_IPC_MB) * 1024 * 1024


def _temp_limit() -> int:
    return _int_env("BTF_SCAN_TEMP_MB", DEFAULT_TEMP_MB) * 1024 * 1024


def _test_hook() -> str:
    if os.environ.get("BTF_SCAN_TEST_HOOKS") == "1":
        return os.environ.get("BTF_SCAN_TEST_HOOK", "")
    return ""


@dataclass(eq=False)
class _Worker:
    process: mp.Process
    connection: Connection
    temp_dir: Path
    jobs: int = 0
    retired: bool = False


@dataclass(eq=False)
class CancellationToken:
    """Identifies one request so cancellation can retire its exact parser worker."""

    cancelled: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self.cancelled.set()
        with _pool_lock:
            pool = _pool
        if pool is not None:
            pool.cancel(self)


class _Pool:
    def __init__(self, size: int) -> None:
        self.size = size
        self._ctx = mp.get_context("spawn")
        self._available: queue.Queue[_Worker] = queue.Queue(maxsize=size)
        self._all: list[_Worker] = []
        self._leases: dict[CancellationToken, _Worker] = {}
        self._closed = False
        self._spawning = 0
        self._state_lock = threading.RLock()
        for _ in range(size):
            worker = self._create_worker()
            self._all.append(worker)
            self._available.put(worker)

    def _create_worker(self) -> _Worker:
        temp_dir = _create_worker_temp_dir()
        parent, child = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=_worker_main, args=(child, os.fspath(temp_dir)), name="btf-media-parser", daemon=True
        )
        try:
            proc.start()
        except BaseException:
            parent.close()
            child.close()
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        child.close()
        return _Worker(proc, parent, temp_dir)

    # Kept as a narrow test hook for deterministic replacement/close races.
    def _spawn(self) -> _Worker:
        return self._create_worker()

    @staticmethod
    def _stop_worker(worker: _Worker) -> None:
        try:
            worker.connection.close()
        except OSError:
            pass
        if worker.process.is_alive():
            worker.process.terminate()
            worker.process.join(0.05)
        if worker.process.is_alive():
            worker.process.kill()
            worker.process.join(0.05)
        else:
            worker.process.join(0.05)
        shutil.rmtree(worker.temp_dir, ignore_errors=True)

    def _remove_locked(self, worker: _Worker) -> bool:
        if worker.retired:
            return False
        worker.retired = True
        if worker in self._all:
            self._all.remove(worker)
        for token, leased in list(self._leases.items()):
            if leased is worker:
                del self._leases[token]
        return True

    def _ensure_capacity(self) -> None:
        """Replace missing capacity; a failed spawn is retried by the next acquire/completion."""
        with self._state_lock:
            if self._closed or self.size - len(self._all) - self._spawning <= 0:
                return
            self._spawning += 1
        try:
            worker = self._spawn()
        except BaseException as exc:
            with self._state_lock:
                self._spawning -= 1
            log.error("could not replace parser worker; pool temporarily degraded: %s", type(exc).__name__)
            return
        with self._state_lock:
            self._spawning -= 1
            late = self._closed
            if late:
                worker.retired = True
            else:
                self._all.append(worker)
                self._available.put(worker)
        if late:
            self._stop_worker(worker)

    def _acquire(self, deadline: float, token: CancellationToken) -> _Worker:
        while True:
            check_deadline(deadline)
            if token.cancelled.is_set():
                raise WorkerFailed("scan cancelled")
            self._ensure_capacity()
            with self._state_lock:
                if self._closed:
                    raise WorkerFailed("parser pool is closed")
            try:
                worker = self._available.get(timeout=min(0.05, remaining(deadline)))
            except queue.Empty:
                continue
            with self._state_lock:
                if self._closed:
                    stop = self._remove_locked(worker)
                elif worker.retired:
                    stop = False
                elif not worker.process.is_alive():
                    stop = self._remove_locked(worker)
                else:
                    self._leases[token] = worker
                    return worker
            if stop:
                self._stop_worker(worker)
                self._ensure_capacity()
                if not self._closed:
                    continue
                raise WorkerFailed("parser pool is closed")

    def _finish(self, token: CancellationToken, worker: _Worker, reusable: bool) -> None:
        with self._state_lock:
            if self._leases.get(token) is worker:
                del self._leases[token]
            if worker.retired:
                return
            if self._closed or not reusable or not worker.process.is_alive():
                stop = self._remove_locked(worker)
            else:
                self._available.put(worker)
                return
        if stop:
            self._stop_worker(worker)
        self._ensure_capacity()

    def run(self, request: dict[str, Any], deadline: float, token: CancellationToken) -> dict[str, Any]:
        worker = self._acquire(deadline, token)
        reusable = True
        try:
            if not worker.process.is_alive():
                reusable = False
                raise WorkerFailed("parser worker exited before its job")
            request["budget_s"] = remaining(deadline)
            payload = _encode_request(request)
            check_deadline(deadline)
            worker.connection.send_bytes(payload)
            wait = remaining(deadline)
            if wait <= 0 or not worker.connection.poll(wait):
                reusable = False
                raise ScanTimeout("parser exhausted the scan time budget")
            try:
                raw = worker.connection.recv_bytes(maxlength=_ipc_limit() + HEADER_MAX + 4)
            except (EOFError, OSError) as exc:
                reusable = False
                raise WorkerFailed("parser worker exited or exceeded the IPC limit") from exc
            check_deadline(deadline)
            response = _decode_response(raw, _ipc_limit(), deadline)
            worker.jobs += 1
            if response["ok"] is not True:
                error = response["error"]
                if error["kind"] == "unsupported_media":
                    raise WorkerUnsupportedMedia(error["message"])
                reusable = False
                raise WorkerFailed(error["message"])
            result = response["result"]
            if worker.jobs >= _int_env("BTF_SCAN_WORKER_JOBS", DEFAULT_MAX_JOBS):
                reusable = False
            return result
        except ScanTimeout:
            reusable = False
            raise
        except WorkerFailed:
            reusable = False
            raise
        except (BrokenPipeError, EOFError, OSError) as exc:
            reusable = False
            raise WorkerFailed("parser worker communication failed") from exc
        finally:
            self._finish(token, worker, reusable)

    def cancel(self, token: CancellationToken) -> None:
        token.cancelled.set()
        with self._state_lock:
            worker = self._leases.get(token)
            removed = self._remove_locked(worker) if worker is not None else False
            replace = not self._closed
        if removed:
            self._stop_worker(worker)
        if removed and replace:
            self._ensure_capacity()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            workers = list(self._all)
            for worker in workers:
                self._remove_locked(worker)
            while True:
                try:
                    self._available.get_nowait()
                except queue.Empty:
                    break
        for worker in workers:
            self._stop_worker(worker)

    def pids(self) -> list[int]:
        with self._state_lock:
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


def parse(
    path: str | Path,
    filename: str,
    content_type: str,
    want_frames: bool,
    *,
    deadline: float | None = None,
    cancellation: CancellationToken | None = None,
) -> dict[str, Any]:
    """Parse one upload in an isolated process within the caller's absolute deadline."""
    deadline = deadline if deadline is not None else deadline_after_timeout()
    cancellation = cancellation or CancellationToken()
    check_deadline(deadline)
    request = {
        "version": PROTOCOL_VERSION,
        "path": os.fspath(path),
        "filename": filename[:255],
        "content_type": content_type[:80],
        "want_frames": bool(want_frames),
        "test_hook": _test_hook(),
        "environment": {name: os.environ.get(name) for name in C2PA_ENV},
    }
    return _get_pool().run(request, deadline, cancellation)


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


def _worker_main(connection: Connection, temp_path: str) -> None:
    # Ignore Ctrl-C intended for the web process; the parent owns worker lifecycle.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    temp_dir = Path(temp_path)
    _activate_worker_temp_dir(temp_dir)
    temp_quota = _temp_limit()
    try:
        _apply_startup_limits(temp_quota)
        _start_resource_watchdog(temp_dir, temp_quota)
        jobs = 0
        max_jobs = _int_env("BTF_SCAN_WORKER_JOBS", DEFAULT_MAX_JOBS)
        while jobs < max_jobs:
            try:
                raw = connection.recv_bytes(maxlength=REQUEST_MAX)
            except (EOFError, OSError):
                break
            try:
                request = _decode_request(raw)
                _apply_job_environment(request["environment"])
                _refresh_cpu_limit(request["budget_s"])
                _check_temp_quota(temp_dir, temp_quota)
                hook = request["test_hook"]
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
                _check_temp_quota(temp_dir, temp_quota)
                response = {"version": PROTOCOL_VERSION, "ok": True, "result": result}
            except BaseException as exc:
                kind = "unsupported_media" if type(exc).__name__ == "UnsupportedMedia" else "scan_failed"
                response = {
                    "version": PROTOCOL_VERSION,
                    "ok": False,
                    "error": {"kind": kind, "message": str(exc)[:500] or type(exc).__name__},
                }
            try:
                encoded = _encode_response(response, _ipc_limit())
            except BaseException:
                encoded = _encode_response(
                    {"version": PROTOCOL_VERSION, "ok": False,
                     "error": {"kind": "scan_failed", "message": "parser result exceeds IPC limit"}},
                    _ipc_limit(),
                )
            try:
                connection.send_bytes(encoded)
            except (BrokenPipeError, EOFError, OSError):
                break
            jobs += 1
    finally:
        connection.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _encode_request(request: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise WorkerFailed("parser request is not JSON-safe") from exc
    if len(payload) > REQUEST_MAX:
        raise WorkerFailed("parser request exceeds IPC limit")
    return payload


def _decode_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > REQUEST_MAX:
        raise ValueError("parser request exceeds IPC limit")
    try:
        request = _strict_json_loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("invalid parser request JSON") from exc
    _validate_request(request)
    return request


def _encode_response(response: dict[str, Any], limit: int) -> bytes:
    frames: list[bytes] = []
    if response.get("ok") is True:
        result = response.get("result")
        _validate_result(result, limit)
        wire_result = dict(result)
        wire_frames = []
        for frame in result["frames"]:
            data = frame["data"]
            frames.append(data)
            wire_frames.append({"width": frame["width"], "height": frame["height"],
                                "mode": "RGB", "length": len(data)})
        wire_result["frames"] = wire_frames
        header_obj = {"version": PROTOCOL_VERSION, "ok": True, "result": wire_result}
    else:
        _validate_error_response(response)
        header_obj = response
    try:
        header = json.dumps(header_obj, ensure_ascii=False, allow_nan=False,
                            separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WorkerFailed("parser response is not JSON-safe") from exc
    if len(header) > HEADER_MAX:
        raise WorkerFailed("parser response metadata exceeds IPC limit")
    if sum(map(len, frames)) > limit:
        raise WorkerFailed("parser RGB payload exceeds IPC limit")
    return struct.pack(">I", len(header)) + header + b"".join(frames)


def _decode_response(raw: bytes, limit: int, deadline: float | None = None) -> dict[str, Any]:
    """Decode a passive framed response after validating every declared length."""
    if len(raw) < 4:
        raise WorkerFailed("parser returned a truncated protocol frame")
    header_len = struct.unpack(">I", raw[:4])[0]
    if header_len < 2 or header_len > HEADER_MAX:
        raise WorkerFailed("parser returned an invalid protocol header length")
    if len(raw) < 4 + header_len:
        raise WorkerFailed("parser returned a truncated protocol header")
    if deadline is not None:
        check_deadline(deadline)
    try:
        header = _strict_json_loads(raw[4:4 + header_len])
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise WorkerFailed("parser returned invalid protocol JSON") from exc
    allowed = ({"version", "ok", "result"}, {"version", "ok", "error"})
    if not isinstance(header, dict) or set(header) not in allowed:
        raise WorkerFailed("parser returned an invalid protocol record")
    if type(header.get("version")) is not int or header["version"] != PROTOCOL_VERSION:
        raise WorkerFailed("parser returned an invalid protocol version")
    if type(header.get("ok")) is not bool:
        raise WorkerFailed("parser returned an invalid protocol status")
    if header["ok"] is False:
        _validate_error_response(header)
        if len(raw) != 4 + header_len:
            raise WorkerFailed("parser returned trailing protocol bytes")
        return header
    if set(header) != {"version", "ok", "result"}:
        raise WorkerFailed("parser returned an invalid success record")
    result = header["result"]
    lengths = _validate_wire_result(result, limit)
    expected = 4 + header_len + sum(lengths)
    if len(raw) < expected:
        raise WorkerFailed("parser returned a truncated RGB payload")
    if len(raw) > expected:
        raise WorkerFailed("parser returned trailing protocol bytes")
    if deadline is not None:
        check_deadline(deadline)
    offset = 4 + header_len
    decoded = []
    for frame, length in zip(result["frames"], lengths):
        decoded.append({"width": frame["width"], "height": frame["height"], "mode": "RGB",
                        "data": raw[offset:offset + length]})
        offset += length
    result["frames"] = decoded
    _validate_result(result, limit)
    if deadline is not None:
        check_deadline(deadline)
    return header


def _strict_json_loads(raw: bytes) -> object:
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)


def _validate_error_response(response: object) -> None:
    if not isinstance(response, dict) or set(response) != {"version", "ok", "error"}:
        raise WorkerFailed("invalid parser error response")
    if type(response.get("version")) is not int or response["version"] != PROTOCOL_VERSION or response.get("ok") is not False:
        raise WorkerFailed("invalid parser error response")
    error = response.get("error")
    if not isinstance(error, dict) or set(error) != {"kind", "message"}:
        raise WorkerFailed("invalid parser error response")
    if error.get("kind") not in {"unsupported_media", "scan_failed"}:
        raise WorkerFailed("invalid parser error kind")
    _string(error.get("message"), "parser error message", 500)


def _validate_request(request: object) -> None:
    keys = {"version", "path", "filename", "content_type", "want_frames", "test_hook", "environment", "budget_s"}
    if not isinstance(request, dict) or set(request) != keys:
        raise ValueError("invalid parser request")
    if type(request["version"]) is not int or request["version"] != PROTOCOL_VERSION:
        raise ValueError("invalid parser request version")
    _string(request["path"], "parser path", 4096)
    _string(request["filename"], "parser filename", 255)
    _string(request["content_type"], "parser content type", 80)
    _string(request["test_hook"], "parser test hook", 32)
    if type(request["want_frames"]) is not bool:
        raise ValueError("invalid parser frame flag")
    if not _finite_number(request["budget_s"], 0.0, DEFAULT_TIMEOUT_S * 100):
        raise ValueError("invalid parser time budget")
    environment = request["environment"]
    if not isinstance(environment, dict) or set(environment) != set(C2PA_ENV):
        raise ValueError("invalid parser environment")
    if any(value is not None and (not isinstance(value, str) or len(value) > MAX_STRING)
           for value in environment.values()):
        raise ValueError("invalid parser environment value")


def _apply_job_environment(environment: dict[str, str | None]) -> None:
    for name in C2PA_ENV:
        value = environment[name]
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


RESULT_KEYS = {
    "version", "kind", "raw_hits", "text_hits", "chunk_hits", "cred", "frames", "timestamps",
    "duration", "width", "height", "camera", "camera_details", "has_metadata", "image_error",
    "video_error", "detector_reason", "low_colour", "notes",
}
HIT_KEYS = {"kind", "key", "text", "weight", "field", "snippet"}
CRED_KEYS = {
    "present", "valid", "trusted", "generator", "issuer", "signer", "signed_at", "capture",
    "claimed_capture", "ai_generated", "ingredient_ai", "actions", "source_types", "software_agents",
    "failures", "ingredients", "trust_list_version", "untrusted", "readable",
}


def _validate_result(result: object, limit: int) -> None:
    _validate_result_metadata(result)
    total = 0
    for frame in result["frames"]:
        if not isinstance(frame, dict) or set(frame) != {"width", "height", "mode", "data"}:
            raise WorkerFailed("invalid parser frame schema")
        expected = _validate_frame_fields(frame)
        data = frame["data"]
        if not isinstance(data, bytes) or len(data) != expected:
            raise WorkerFailed("invalid parser RGB payload")
        total += expected
        if total > limit:
            raise WorkerFailed("parser RGB payload exceeds IPC limit")


def _validate_wire_result(result: object, limit: int) -> list[int]:
    _validate_result_metadata(result)
    lengths, total = [], 0
    for frame in result["frames"]:
        if not isinstance(frame, dict) or set(frame) != {"width", "height", "mode", "length"}:
            raise WorkerFailed("invalid parser frame schema")
        expected = _validate_frame_fields(frame)
        if type(frame["length"]) is not int or frame["length"] != expected:
            raise WorkerFailed("invalid parser RGB length")
        total += expected
        if total > limit:
            raise WorkerFailed("parser RGB payload exceeds IPC limit")
        lengths.append(expected)
    return lengths


def _validate_result_metadata(result: object) -> None:
    if not isinstance(result, dict) or set(result) != RESULT_KEYS:
        raise WorkerFailed("invalid parser result schema")
    if type(result["version"]) is not int or result["version"] != PROTOCOL_VERSION:
        raise WorkerFailed("invalid parser result version")
    if result["kind"] not in {"image", "video"}:
        raise WorkerFailed("invalid parser media kind")
    frames = result["frames"]
    if not isinstance(frames, list) or len(frames) > 8:
        raise WorkerFailed("invalid parser frame list")
    for name in ("raw_hits", "text_hits", "chunk_hits"):
        _validate_hits(result[name], name)
    _validate_cred(result["cred"])
    timestamps = result["timestamps"]
    if not isinstance(timestamps, list) or len(timestamps) > 8:
        raise WorkerFailed("invalid parser timestamps")
    if any(not _finite_number(value, 0.0, 10 * 365 * 24 * 3600) for value in timestamps):
        raise WorkerFailed("invalid parser timestamp")
    duration = result["duration"]
    if duration is not None and not _finite_number(duration, 0.0, 10 * 365 * 24 * 3600):
        raise WorkerFailed("invalid parser duration")
    width, height = result["width"], result["height"]
    if (width is None) != (height is None):
        raise WorkerFailed("inconsistent parser dimensions")
    if width is not None:
        if not _bounded_int(width, 1, 1_000_000) or not _bounded_int(height, 1, 1_000_000):
            raise WorkerFailed("invalid parser dimensions")
        if width * height > _int_env("BTF_MAX_IMAGE_PIXELS", 120_000_000):
            raise WorkerFailed("parser dimensions exceed pixel limit")
    for name in ("camera_details", "has_metadata", "image_error", "low_colour"):
        if type(result[name]) is not bool:
            raise WorkerFailed(f"invalid parser boolean {name}")
    _string(result["camera"], "parser camera", 1024)
    _string(result["detector_reason"], "parser detector reason", 1024)
    if result["video_error"] is not None:
        _string(result["video_error"], "parser video error", 1024)
    notes = result["notes"]
    if not isinstance(notes, list) or len(notes) > MAX_NOTES:
        raise WorkerFailed("invalid parser notes")
    for note in notes:
        _string(note, "parser note", 1024)
    if result["kind"] == "image":
        if timestamps or duration is not None or result["video_error"] is not None or len(frames) > 1:
            raise WorkerFailed("inconsistent image result")
    else:
        if len(timestamps) != len(frames):
            raise WorkerFailed("video frame/timestamp count mismatch")
        if duration is not None and timestamps and timestamps[-1] > duration + 1.0:
            raise WorkerFailed("video timestamp exceeds duration")


def _validate_frame_fields(frame: dict[str, Any]) -> int:
    if frame.get("mode") != "RGB":
        raise WorkerFailed("invalid parser frame mode")
    width, height = frame.get("width"), frame.get("height")
    if not _bounded_int(width, 1, 1_000_000) or not _bounded_int(height, 1, 1_000_000):
        raise WorkerFailed("invalid parser frame dimensions")
    if width * height > _int_env("BTF_MAX_DECODE_PIXELS", DEFAULT_MAX_DECODE_PIXELS):
        raise WorkerFailed("parser frame exceeds the pixel budget")
    return width * height * 3


def _validate_hits(value: object, name: str) -> None:
    if not isinstance(value, list) or len(value) > MAX_HITS:
        raise WorkerFailed(f"invalid parser {name}")
    for hit in value:
        if not isinstance(hit, dict) or set(hit) != HIT_KEYS or hit["kind"] not in {"ai", "edit", "info"}:
            raise WorkerFailed("invalid parser hit schema")
        _string(hit["key"], "parser hit key", 128)
        _string(hit["text"], "parser hit text", MAX_STRING)
        _string(hit["field"], "parser hit field", 256)
        _string(hit["snippet"], "parser hit snippet", 80)
        if not _finite_number(hit["weight"], 0.0, 1.0):
            raise WorkerFailed("invalid parser hit weight")


def _validate_cred(cred: object) -> None:
    if cred is None:
        return
    if not isinstance(cred, dict) or set(cred) != CRED_KEYS:
        raise WorkerFailed("invalid parser credential schema")
    for name in ("present", "valid", "trusted", "capture", "claimed_capture", "ai_generated",
                 "ingredient_ai", "untrusted", "readable"):
        if type(cred[name]) is not bool:
            raise WorkerFailed(f"invalid parser credential boolean {name}")
    for name in ("generator", "issuer", "signer", "signed_at", "trust_list_version"):
        _string(cred[name], f"parser credential {name}", 1024)
    for name in ("actions", "source_types", "software_agents", "failures"):
        values = cred[name]
        if not isinstance(values, list) or len(values) > MAX_CRED_ITEMS:
            raise WorkerFailed(f"invalid parser credential {name}")
        for value in values:
            _string(value, f"parser credential {name} value", 1024)
    ingredients = cred["ingredients"]
    if not isinstance(ingredients, list) or len(ingredients) > MAX_INGREDIENTS:
        raise WorkerFailed("invalid parser credential ingredients")
    for ingredient in ingredients:
        allowed = {"title", "format", "relationship", "ai_generated"}
        if not isinstance(ingredient, dict) or not ingredient or not set(ingredient) <= allowed:
            raise WorkerFailed("invalid parser credential ingredient")
        for key, value in ingredient.items():
            _string(key, "parser ingredient key", 128)
            _string(value, "parser ingredient value", 1024)
        if "ai_generated" in ingredient and ingredient["ai_generated"] != "true":
            raise WorkerFailed("invalid parser credential ingredient AI flag")


def _string(value: object, name: str, maximum: int) -> None:
    if not isinstance(value, str) or len(value) > maximum:
        raise WorkerFailed(f"invalid {name}")


def _bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _finite_number(value: object, minimum: float, maximum: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def _make_worker_temp_dir() -> Path:
    path = _create_worker_temp_dir()
    _activate_worker_temp_dir(path)
    return path


def _create_worker_temp_dir() -> Path:
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    path = Path(tempfile.mkdtemp(prefix="btf-parser-", dir=base))
    path.chmod(0o700)
    return path


def _activate_worker_temp_dir(path: Path) -> None:
    for name in ("TMPDIR", "TMP", "TEMP"):
        os.environ[name] = os.fspath(path)
    tempfile.tempdir = os.fspath(path)


def _temp_usage(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name), follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def _check_temp_quota(path: Path, maximum: int) -> None:
    if _temp_usage(path) > maximum:
        raise OSError("parser temporary-file quota exceeded")


def _apply_startup_limits(temp_quota: int) -> None:
    rss_bytes = _int_env("BTF_SCAN_MAX_RSS_MB", DEFAULT_MAX_RSS_MB) * 1024 * 1024
    _set_limit("RLIMIT_AS", rss_bytes)
    _set_limit("RLIMIT_FSIZE", temp_quota)
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


def _start_resource_watchdog(temp_dir: Path, temp_quota: int) -> None:
    maximum = _int_env("BTF_SCAN_MAX_RSS_MB", DEFAULT_MAX_RSS_MB) * 1024 * 1024

    def watch() -> None:
        while True:
            if _rss_bytes() > maximum:
                os._exit(87)
            if _temp_usage(temp_dir) > temp_quota:
                os._exit(88)
            time.sleep(0.1)

    threading.Thread(target=watch, name="btf-parser-resource", daemon=True).start()
