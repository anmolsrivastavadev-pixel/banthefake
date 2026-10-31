"""Upload edge cases: client disconnects, unterminated multipart bodies, audio-only MP4s, test temp dirs."""

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests import media

BOUNDARY = "btfb"


def multipart(data: bytes, close: bool = True) -> bytes:
    head = (f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.png\"\r\n"
            "Content-Type: image/png\r\n\r\n").encode()
    return head + data + (f"\r\n--{BOUNDARY}--\r\n".encode() if close else b"")


def asgi_call(app, method, path, headers, chunks, delay_s=0.0, complete=False):
    """Drive the ASGI app directly, optionally delaying chunks or completing instead of disconnecting."""
    sent: list[dict] = []
    queue = [{"type": "http.request", "body": c, "more_body": True} for c in chunks]
    if complete:
        queue[-1]["more_body"] = False
    else:
        queue.append({"type": "http.disconnect"})

    async def receive():
        if delay_s:
            await asyncio.sleep(delay_s)
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    return sent


def test_scan_client_disconnect_is_not_logged_as_error(client, caplog):
    body = multipart(media.png())[:120]
    with caplog.at_level(logging.INFO):
        sent = asgi_call(client.app, "POST", "/api/scan",
                         {"content-type": f"multipart/form-data; boundary={BOUNDARY}", "content-length": "100000"}, [body])
    assert sent[0]["status"] == 400
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]
    assert any("disconnect" in r.getMessage() for r in caplog.records)


def test_petition_client_disconnect_400(client, caplog):
    with caplog.at_level(logging.INFO):
        sent = asgi_call(client.app, "POST", "/api/petition",
                         {"content-type": "application/json", "content-length": "500"}, [b'{"name": "A'])
    assert sent[0]["status"] == 400
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]


def test_multipart_without_closing_boundary_400(client):
    res = client.post("/api/scan", content=multipart(media.png(), close=False),
                      headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"})
    assert res.status_code == 400 and "incomplete" in res.json()["error"]


def test_multipart_with_closing_boundary_ok(client):
    res = client.post("/api/scan", content=multipart(media.png()),
                      headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"})
    assert res.status_code == 200


def test_slow_upload_does_not_consume_scan_budget(make_client, monkeypatch):
    monkeypatch.setenv("BTF_SCAN_TIMEOUT_S", "0.05")
    monkeypatch.setattr("app.scan.scan_file", lambda path, filename, mime: {"verdict": "unknown"})
    body = multipart(media.png())
    chunks = [body[:80], body[80:160], body[160:]]
    started = time.monotonic()
    sent = asgi_call(
        make_client(upload_timeout_s=1).app,
        "POST",
        "/api/scan",
        {"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
        chunks,
        delay_s=0.03,
        complete=True,
    )
    assert time.monotonic() - started > 0.05
    assert sent[0]["status"] == 200


def test_upload_timeout_408_and_deletes_temp_file(make_client, monkeypatch):
    created = []
    real_mkstemp = tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(Path(name))
        return fd, name

    monkeypatch.setattr("app.main.tempfile.mkstemp", tracked_mkstemp)
    body = multipart(media.png())
    sent = asgi_call(
        make_client(upload_timeout_s=0.01).app,
        "POST",
        "/api/scan",
        {"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
        [body],
        delay_s=0.03,
        complete=True,
    )
    assert sent[0]["status"] == 408
    assert b'"error":"upload_timeout"' in sent[1]["body"]
    assert created and all(not path.exists() for path in created)


def test_audio_only_mp4_415(client):
    data = media.audio_only_mp4()
    res = client.post("/api/scan", files={"file": ("song.mp4", data, "video/mp4")})
    assert res.status_code == 415
    assert "no video track" in res.json()["error"] and "audio-only" in res.json()["error"]


def test_conftest_removes_its_temp_dir(tmp_path):
    """Running the suite must not leave btf-test-* directories behind in the temp dir."""
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    env.pop("BTF_DB_PATH", None)
    root = Path(__file__).resolve().parent.parent
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_misc.py::test_health"],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert list(tmp_path.glob("btf-test-*")) == []
