"""End-to-end checks for hostile-media process isolation."""

from __future__ import annotations

import asyncio
import copy
import json
import multiprocessing as mp
import os
import pickle
import statistics
import struct
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app import scan
from app.config import Settings
import app.main as mainmod
from app.main import create_app
from app.scan import isolate, model
from tests import media
from tests.conftest import FIXTURES
from tests.test_concurrency import run_parallel


class _MaliciousPickle:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    def __reduce__(self):
        return os.system, (f"touch {self.marker}",)


def _valid_result(tmp_path: Path, *, frames: bool = False) -> dict:
    path = tmp_path / "schema.png"
    path.write_bytes(media.png())
    return scan._parse_hostile(path, "image/png", frames)


@pytest.fixture(autouse=True)
def isolated_pool(monkeypatch):
    isolate.shutdown()
    monkeypatch.setenv("BTF_SCAN_ISOLATION", "on")
    yield
    pids = isolate.active_worker_pids()
    isolate.shutdown()
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_hung_worker_is_killed_returns_504_and_deletes_upload(make_client, monkeypatch):
    monkeypatch.setenv("BTF_SCAN_TEST_HOOKS", "1")
    monkeypatch.setenv("BTF_SCAN_TEST_HOOK", "hang")
    monkeypatch.setenv("BTF_SCAN_TIMEOUT_S", "0.2")
    created: list[Path] = []
    real_mkstemp = tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(Path(name))
        return fd, name

    monkeypatch.setattr("app.main.tempfile.mkstemp", tracked_mkstemp)
    started = time.perf_counter()
    response = make_client().post(
        "/api/scan", files={"file": ("hang.png", media.png(), "image/png")}
    )
    elapsed = time.perf_counter() - started
    print(f"hung parser deadline test: configured=0.20s response={elapsed:.3f}s")

    assert response.status_code == 504
    assert response.json() == {"error": "scan_timeout"}
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert elapsed < 2.0
    assert created and all(not path.exists() for path in created)


def test_non_jpeg_over_decode_budget_is_not_fully_decoded(make_client, monkeypatch):
    isolate.shutdown()  # the spawned worker must import exif with this deterministic test budget
    monkeypatch.setenv("BTF_MAX_DECODE_PIXELS", "10000")
    monkeypatch.setattr(model, "enabled", lambda: True)
    calls = []

    def should_not_run(images):
        calls.append(images)
        raise AssertionError("oversized PNG reached model inference")

    monkeypatch.setattr(model, "predict", should_not_run)
    response = make_client().post(
        "/api/scan", files={"file": ("large.png", media.photo_png(), "image/png")}
    )
    body = response.json()
    assert response.status_code == 200
    assert body["model"] is None and not calls
    assert any(
        "above the 0 MP the detector can decode" in signal["text"]
        for signal in body["signals"]
        if signal["kind"] == "info"
    )


def test_worker_crash_is_clean_and_pool_recovers(make_client, monkeypatch):
    monkeypatch.setenv("BTF_SCAN_TEST_HOOKS", "1")
    monkeypatch.setenv("BTF_SCAN_TEST_HOOK", "crash")
    client = make_client()
    failed = client.post("/api/scan", files={"file": ("x.png", media.png(), "image/png")})
    assert failed.status_code == 500 and failed.json() == {"error": "scan_failed"}
    assert failed.headers["x-frame-options"] == "DENY"

    monkeypatch.delenv("BTF_SCAN_TEST_HOOK")
    recovered = client.post("/api/scan", files={"file": ("x.png", media.png(), "image/png")})
    assert recovered.status_code == 200
    assert recovered.json()["verdict"] == "unknown"


def test_worker_recycles_after_configured_job_count(tmp_path, monkeypatch):
    monkeypatch.setenv("BTF_SCAN_WORKER_JOBS", "1")
    scan.configure_isolation(1)
    path = tmp_path / "x.png"
    path.write_bytes(media.png())
    isolate.parse(path, path.name, "image/png", want_frames=False)
    first_replacement = isolate.active_worker_pids()
    assert len(first_replacement) == 1
    isolate.parse(path, path.name, "image/png", want_frames=False)
    second_replacement = isolate.active_worker_pids()
    assert len(second_replacement) == 1 and second_replacement != first_replacement


def test_api_concurrency_semaphore_still_holds(tmp_path, monkeypatch):
    responses, peak = run_parallel(tmp_path, monkeypatch, 6, scan_concurrency=2, scan_wait_s=30)
    assert [response.status_code for response in responses] == [200] * 6
    assert peak == 2


def _fixture_media() -> list[Path]:
    return sorted(
        path for path in FIXTURES.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm"}
    )


def _mime(path: Path) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    }[path.suffix.lower()]


def test_isolation_parity_for_every_media_fixture_and_latency(monkeypatch):
    fixtures = _fixture_media()
    assert fixtures
    monkeypatch.setattr(model, "enabled", lambda: True)
    monkeypatch.setattr(model, "predict", media.fake_predict(0.1, 0.2))
    scan.configure_isolation(2)

    # Warm both code paths and the worker before timing. Alternate order per fixture so filesystem cache
    # effects do not systematically favour either mode.
    warm = fixtures[0]
    monkeypatch.setenv("BTF_SCAN_ISOLATION", "off")
    scan.scan_file(warm, warm.name, _mime(warm))
    monkeypatch.setenv("BTF_SCAN_ISOLATION", "on")
    scan.scan_file(warm, warm.name, _mime(warm))

    off_times, on_times = [], []

    def measured(path: Path, enabled: bool) -> tuple[dict, float]:
        monkeypatch.setenv("BTF_SCAN_ISOLATION", "on" if enabled else "off")
        started = time.perf_counter()
        report = scan.scan_file(path, path.name, _mime(path))
        return report, time.perf_counter() - started

    for index, path in enumerate(fixtures):
        if index % 2:
            on_report, on_elapsed = measured(path, True)
            off_report, off_elapsed = measured(path, False)
        else:
            off_report, off_elapsed = measured(path, False)
            on_report, on_elapsed = measured(path, True)
        off_times.append(off_elapsed)
        on_times.append(on_elapsed)
        before = json.dumps(off_report, sort_keys=True, separators=(",", ":")).encode()
        after = json.dumps(on_report, sort_keys=True, separators=(",", ":")).encode()
        assert after == before, path.relative_to(FIXTURES)
        assert all(key in on_report for key in ("verdict", "signals", "c2pa", "model"))

    off_ms = statistics.median(off_times) * 1000
    on_ms = statistics.median(on_times) * 1000
    print(
        f"isolation latency median over {len(fixtures)} fixtures: "
        f"off={off_ms:.2f}ms on={on_ms:.2f}ms overhead={on_ms-off_ms:.2f}ms"
    )
    assert on_ms - off_ms < 2000


def test_shutdown_reaps_all_workers(tmp_path):
    path = tmp_path / "x.png"
    path.write_bytes(media.png())
    isolate.parse(path, path.name, "image/png", want_frames=False)
    pids = isolate.active_worker_pids()
    assert pids
    isolate.shutdown()
    assert isolate.active_worker_pids() == []
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_result_protocol_rejects_pickle_without_executing_it(tmp_path):
    marker = tmp_path / "pickle-executed"
    payload = pickle.dumps(_MaliciousPickle(str(marker)), protocol=5)
    parent, child = mp.Pipe(duplex=False)
    writer = threading.Thread(target=lambda: (child.send_bytes(payload), child.close()))
    writer.start()
    raw = parent.recv_bytes()
    writer.join()
    parent.close()

    with pytest.raises(isolate.WorkerFailed, match="protocol"):
        isolate._decode_response(raw, 1024)
    assert not marker.exists(), "the parent must never unpickle a worker response"


def test_result_protocol_rejects_truncated_oversized_and_trailing_frames(tmp_path):
    result = _valid_result(tmp_path, frames=True)
    framed = isolate._encode_response(
        {"version": isolate.PROTOCOL_VERSION, "ok": True, "result": result}, 10 * 1024 * 1024
    )
    bad = {
        "truncated header": framed[:3],
        "oversized header": struct.pack(">I", isolate.HEADER_MAX + 1) + b"{}",
        "missing RGB": framed[:-1],
        "trailing byte": framed + b"x",
    }
    for payload in bad.values():
        with pytest.raises(isolate.WorkerFailed):
            isolate._decode_response(payload, 10 * 1024 * 1024)


def test_end_to_end_deadline_skips_slow_inference_within_slack(tmp_path, monkeypatch):
    path = tmp_path / "deadline.png"
    path.write_bytes(media.png())
    monkeypatch.setenv("BTF_SCAN_TIMEOUT_S", "2")
    isolate.parse(path, path.name, "image/png", False)  # warm the spawned process
    monkeypatch.setenv("BTF_SCAN_TIMEOUT_S", "0.12")
    monkeypatch.setenv("BTF_SCAN_INFER_RESERVE_S", "0.20")
    monkeypatch.setattr(model, "enabled", lambda: True)
    called = []

    def slow_predict(images):
        called.append(True)
        time.sleep(0.5)
        return model.Prediction(h=[0.1] * len(images), c=[0.1] * len(images))

    monkeypatch.setattr(model, "predict", slow_predict)
    started = time.monotonic()
    report = scan.scan_file(path, path.name, "image/png")
    elapsed = time.monotonic() - started
    assert elapsed <= 0.27
    assert not called
    assert any("time budget" in signal["text"] for signal in report["signals"])


def test_production_inference_uses_small_batches_and_reserve_between_calls(monkeypatch):
    calls = []

    def fake_predict(images):
        calls.append(len(images))
        return model.Prediction(h=[0.1] * len(images), c=[0.1] * len(images))

    monkeypatch.setattr(model, "predict", fake_predict)
    monkeypatch.setattr(scan, "_MODEL_PREDICT_ORIGINAL", fake_predict)
    monkeypatch.setenv("BTF_SCAN_INFER_BATCH", "2")
    monkeypatch.setenv("BTF_SCAN_INFER_RESERVE_S", "0.05")
    pred = scan._predict_in_batches([object()] * 5, time.monotonic() + 1)
    assert pred is not None and len(pred) == 5 and calls == [2, 2, 1]

    calls.clear()

    def slow_predict(images):
        calls.append(len(images))
        time.sleep(0.04)
        return model.Prediction(h=[0.1] * len(images), c=[0.1] * len(images))

    monkeypatch.setattr(model, "predict", slow_predict)
    monkeypatch.setattr(scan, "_MODEL_PREDICT_ORIGINAL", slow_predict)
    pred = scan._predict_in_batches([object()] * 5, time.monotonic() + 0.08)
    assert pred is not None and len(pred) == 2 and calls == [2]


def test_pool_queue_uses_each_scan_absolute_deadline(tmp_path, monkeypatch):
    path = tmp_path / "queue.png"
    path.write_bytes(media.png())
    monkeypatch.setenv("BTF_SCAN_TEST_HOOKS", "1")
    monkeypatch.setenv("BTF_SCAN_TEST_HOOK", "hang")
    monkeypatch.setenv("BTF_SCAN_TIMEOUT_S", "0.20")
    scan.configure_isolation(1)
    elapsed = []

    def call():
        started = time.monotonic()
        with pytest.raises(isolate.ScanTimeout):
            isolate.parse(path, path.name, "image/png", False)
        elapsed.append(time.monotonic() - started)

    first = threading.Thread(target=call)
    second = threading.Thread(target=call)
    first.start()
    time.sleep(0.07)
    second.start()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert len(elapsed) == 2 and max(elapsed) <= 0.35


def test_close_during_replacement_leaves_no_late_child(tmp_path, monkeypatch):
    path = tmp_path / "race.png"
    path.write_bytes(media.png())
    monkeypatch.setenv("BTF_SCAN_TEST_HOOKS", "1")
    monkeypatch.setenv("BTF_SCAN_TEST_HOOK", "hang")
    monkeypatch.setenv("BTF_SCAN_TIMEOUT_S", "0.08")
    scan.configure_isolation(1)
    pool = isolate._get_pool()
    original = pool._spawn
    entered, release = threading.Event(), threading.Event()

    def delayed_spawn():
        entered.set()
        release.wait(2)
        return original()

    monkeypatch.setattr(pool, "_spawn", delayed_spawn)
    outcome = []

    def job():
        try:
            isolate.parse(path, path.name, "image/png", False)
        except Exception as exc:
            outcome.append(type(exc).__name__)

    thread = threading.Thread(target=job)
    thread.start()
    assert entered.wait(2)
    started = time.monotonic()
    pool.close()
    assert time.monotonic() - started < 0.2
    release.set()
    thread.join(2)
    assert outcome == ["ScanTimeout"]
    assert not thread.is_alive() and pool.pids() == []


def test_replacement_spawn_failure_is_retried_on_next_acquire(tmp_path, monkeypatch):
    path = tmp_path / "retry.png"
    path.write_bytes(media.png())
    monkeypatch.setenv("BTF_SCAN_TEST_HOOKS", "1")
    monkeypatch.setenv("BTF_SCAN_TEST_HOOK", "hang")
    monkeypatch.setenv("BTF_SCAN_TIMEOUT_S", "0.08")
    scan.configure_isolation(1)
    pool = isolate._get_pool()
    original = pool._spawn
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic spawn failure")
        return original()

    monkeypatch.setattr(pool, "_spawn", fail_once)
    with pytest.raises(isolate.ScanTimeout):
        isolate.parse(path, path.name, "image/png", False)
    monkeypatch.delenv("BTF_SCAN_TEST_HOOK")
    # Worker startup can take several seconds on a loaded host.  This operation verifies that the
    # next acquire retries the failed replacement; its wall-clock duration is not part of the
    # behaviour under test, so retain the normal production timeout as a generous backstop.
    monkeypatch.setenv("BTF_SCAN_TIMEOUT_S", "30")
    result = isolate.parse(path, path.name, "image/png", False)
    assert result["kind"] == "image" and len(pool.pids()) == 1 and calls >= 2


def test_request_cancellation_retires_worker_restores_capacity_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("BTF_SCAN_TEST_HOOKS", "1")
    monkeypatch.setenv("BTF_SCAN_TEST_HOOK", "hang")
    monkeypatch.setenv("BTF_SCAN_TIMEOUT_S", "2")
    created = []
    real_mkstemp = tempfile.mkstemp

    def tracked(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(Path(name))
        return fd, name

    monkeypatch.setattr(mainmod.tempfile, "mkstemp", tracked)
    app = create_app(replace(Settings(db_path=tmp_path / "cancel.db", scan_rate=100), scan_concurrency=1))

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            task = asyncio.create_task(client.post(
                "/api/scan", files={"file": ("x.png", media.png(), "image/png")}
            ))
            while not isolate.active_worker_pids():
                await asyncio.sleep(0.005)
            old = isolate.active_worker_pids()[0]
            pool = isolate._get_pool()
            with pool._state_lock:
                old_temp_dir = next(worker.temp_dir for worker in pool._all if worker.process.pid == old)
            started = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            while old in isolate.active_worker_pids() and time.monotonic() - started < 0.3:
                await asyncio.sleep(0.005)
            assert old not in isolate.active_worker_pids()
            assert not old_temp_dir.exists()
            while not isolate.active_worker_pids() and time.monotonic() - started < 0.3:
                await asyncio.sleep(0.005)
            assert isolate.active_worker_pids() and time.monotonic() - started < 0.3
            assert created and all(not path.exists() for path in created)
            monkeypatch.delenv("BTF_SCAN_TEST_HOOK")
            response = await client.post(
                "/api/scan", files={"file": ("ok.png", media.png(), "image/png")}
            )
            assert response.status_code == 200

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("missing key", lambda r: r.pop("camera")),
        ("extra key", lambda r: r.update(extra=True)),
        ("boolean as int", lambda r: r.update(has_metadata=1)),
        ("nonfinite numeric", lambda r: r.update(duration=float("nan"), kind="video")),
        ("negative duration", lambda r: r.update(duration=-1.0, kind="video")),
        ("oversized string", lambda r: r.update(camera="x" * 1025)),
        ("oversized list", lambda r: r.update(notes=[""] * (isolate.MAX_NOTES + 1))),
        ("half dimensions", lambda r: r.update(width=1, height=None)),
        ("frame timestamp mismatch", lambda r: r.update(kind="video", timestamps=[0.0])),
        ("frame bytes mismatch", lambda r: r.update(frames=[{"width": 1, "height": 1, "mode": "RGB", "data": b"x"}])),
    ],
)
def test_full_result_validation_rejects_every_class(tmp_path, case, mutate):
    result = copy.deepcopy(_valid_result(tmp_path))
    mutate(result)
    with pytest.raises(isolate.WorkerFailed):
        isolate._validate_result(result, 1024 * 1024)


def test_result_protocol_caps_total_metadata_bytes(tmp_path):
    result = _valid_result(tmp_path)
    result["cred"] = {
        "present": True, "valid": False, "trusted": False, "generator": "", "issuer": "",
        "signer": "", "signed_at": "", "capture": False, "claimed_capture": False,
        "ai_generated": False, "ingredient_ai": False,
        "actions": ["x" * 1024] * isolate.MAX_CRED_ITEMS,
        "source_types": ["x" * 1024] * isolate.MAX_CRED_ITEMS,
        "software_agents": ["x" * 1024] * isolate.MAX_CRED_ITEMS,
        "failures": [], "ingredients": [], "trust_list_version": "", "untrusted": False,
        "readable": True,
    }
    with pytest.raises(isolate.WorkerFailed, match="metadata"):
        isolate._encode_response({"version": 1, "ok": True, "result": result}, 1024 * 1024)


def test_worker_temp_quota_replaces_zero_file_limit(monkeypatch, tmp_path):
    applied = []
    monkeypatch.setattr(isolate, "_set_limit", lambda name, value: applied.append((name, value)))
    isolate._apply_startup_limits(123456)
    assert ("RLIMIT_FSIZE", 123456) in applied
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    old_tempdir = tempfile.tempdir
    private = isolate._make_worker_temp_dir()
    try:
        assert private.parent == tmp_path
        assert os.environ["TMPDIR"] == str(private)
        assert private.stat().st_mode & 0o777 == 0o700
    finally:
        import shutil
        shutil.rmtree(private, ignore_errors=True)
        tempfile.tempdir = old_tempdir


@pytest.mark.parametrize(
    ("fixture", "mime"),
    [
        (FIXTURES / "probe_c2pa" / "s_capture.jpg", "image/jpeg"),
        (FIXTURES / "test_clip.mp4", "video/mp4"),
    ],
)
def test_worker_temp_quota_keeps_c2pa_and_pyav_parsing(fixture, mime):
    result = isolate.parse(fixture, fixture.name, mime, False)
    assert result["kind"] in {"image", "video"}
