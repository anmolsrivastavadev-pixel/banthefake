"""End-to-end checks for hostile-media process isolation."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
from pathlib import Path

import pytest

from app import scan
from app.scan import isolate, model
from tests import media
from tests.conftest import FIXTURES
from tests.test_concurrency import run_parallel


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
