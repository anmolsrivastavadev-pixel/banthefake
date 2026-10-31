"""Issue: unbounded parallel scans could decode many large images at once and exhaust memory."""

import asyncio
import threading
import time
from dataclasses import replace

import httpx

from app.config import Settings
from app.main import create_app
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
    responses, peak = run_parallel(tmp_path, monkeypatch, 3, scan_concurrency=1, scan_wait_s=0.1)
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 503, 503] and peak == 1
    busy = next(r for r in responses if r.status_code == 503)
    assert busy.json() == {"error": "busy"} and busy.headers["retry-after"]
    assert busy.headers["x-frame-options"] == "DENY"


def test_concurrency_settings_from_env(monkeypatch):
    assert Settings().scan_concurrency == 2 and Settings().scan_wait_s == 30.0
    monkeypatch.setenv("BTF_SCAN_CONCURRENCY", "4")
    monkeypatch.setenv("BTF_SCAN_WAIT_S", "5")
    s = Settings.from_env()
    assert s.scan_concurrency == 4 and s.scan_wait_s == 5.0
