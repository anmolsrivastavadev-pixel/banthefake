"""Issue: unbounded parallel scans could decode many large images at once and exhaust memory."""

import asyncio
import threading
import time
from dataclasses import replace

import httpx

from app import scan
from app.config import Settings
from app.main import ScanSlots, create_app
from tests import media


def run_parallel(tmp_path, monkeypatch, n_requests, **settings):
    running = 0
    peak = 0
    lock = threading.Lock()

    def slow_scan(path, filename, mime):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.4)
        with lock:
            running -= 1
        return {"verdict": "unknown"}

    monkeypatch.setattr("app.scan.scan_file", slow_scan)
    app = create_app(replace(Settings(db_path=tmp_path / "t.db", scan_rate=1000), **settings))
    data = media.png()

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return await asyncio.gather(
                *(c.post("/api/scan", files={"file": ("x.png", data, "image/png")}) for _ in range(n_requests))
            )

    return asyncio.run(main()), peak


def test_scans_are_limited_to_configured_concurrency(tmp_path, monkeypatch):
    responses, peak = run_parallel(tmp_path, monkeypatch, 6, scan_concurrency=2, scan_wait_s=30)
    assert [r.status_code for r in responses] == [200] * 6
    assert peak == 2


def test_busy_503_when_queue_wait_too_long(tmp_path, monkeypatch):
    now = 0.0

    def deadline_after_timeout():
        return now + scan.isolate.timeout_s()

    def remaining(deadline):
        return max(0.0, deadline - now)

    async def exhaust_wait(self, timeout):
        nonlocal now
        assert timeout == Settings().scan_wait_s
        now += timeout
        return False

    monkeypatch.delenv("BTF_SCAN_TIMEOUT_S", raising=False)
    monkeypatch.setattr(scan.isolate, "deadline_after_timeout", deadline_after_timeout)
    monkeypatch.setattr(scan.isolate, "remaining", remaining)
    monkeypatch.setattr(ScanSlots, "acquire", exhaust_wait)
    app = create_app(Settings(db_path=tmp_path / "busy.db", scan_rate=1000))

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post("/api/scan", files={"file": ("x.png", media.png(), "image/png")})

    busy = asyncio.run(request())
    assert busy.status_code == 503
    assert busy.json() == {"error": "busy"} and busy.headers["retry-after"]
    assert busy.headers["x-frame-options"] == "DENY"


def test_scan_gets_full_deadline_after_queue_wait(tmp_path, monkeypatch):
    now = 100.0
    budgets = []

    def deadline_after_timeout():
        return now + scan.isolate.timeout_s()

    def remaining(deadline):
        return max(0.0, deadline - now)

    async def wait_then_acquire(self, timeout):
        nonlocal now
        now += 17.0
        return True

    def record_budget(path, filename, mime):
        budgets.append(remaining(scan._DEADLINE_CONTEXT.get()))
        return {"verdict": "unknown"}

    monkeypatch.delenv("BTF_SCAN_TIMEOUT_S", raising=False)
    monkeypatch.setattr(scan.isolate, "deadline_after_timeout", deadline_after_timeout)
    monkeypatch.setattr(scan.isolate, "remaining", remaining)
    monkeypatch.setattr(ScanSlots, "acquire", wait_then_acquire)
    monkeypatch.setattr(scan, "scan_file", record_budget)
    app = create_app(Settings(db_path=tmp_path / "deadline.db", scan_rate=1000))

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post("/api/scan", files={"file": ("x.png", media.png(), "image/png")})

    response = asyncio.run(request())
    assert response.status_code == 200
    assert budgets == [scan.isolate.DEFAULT_TIMEOUT_S]


def test_concurrency_settings_from_env(monkeypatch):
    assert Settings().upload_timeout_s == 60.0
    assert Settings().scan_concurrency == 2 and Settings().scan_wait_s == 30.0
    monkeypatch.setenv("BTF_UPLOAD_TIMEOUT_S", "45")
    monkeypatch.setenv("BTF_SCAN_CONCURRENCY", "4")
    monkeypatch.setenv("BTF_SCAN_WAIT_S", "5")
    s = Settings.from_env()
    assert s.upload_timeout_s == 45.0 and s.scan_concurrency == 4 and s.scan_wait_s == 5.0
