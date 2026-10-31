import io
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from app.sniff import sniff


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return buf.getvalue()


def temp_files() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("btf-*")}


def test_scan_returns_report(client):
    data = png_bytes()
    res = client.post("/api/scan", files={"file": ("x.png", data, "image/png")})
    assert res.status_code == 200
    body = res.json()
    assert body["verdict"] in {"verified_capture", "verified_ai", "ai", "edited", "camera", "unknown"}
    assert {"verdict_title", "verdict_detail", "signals", "c2pa", "model", "file"} <= body.keys()
    assert body["file"] == {"type": "image", "mime": "image/png", "size_bytes": len(data), "width": 8, "height": 8}


def test_scan_oversize_413(make_client):
    client = make_client(max_upload_mb=1)
    blob = b"\xff\xd8\xff" + b"\0" * (1024 * 1024 + 10)
    res = client.post("/api/scan", files={"file": ("big.jpg", blob, "image/jpeg")})
    assert res.status_code == 413
    assert "error" in res.json()


def test_scan_rejects_text_named_jpg_415(client):
    res = client.post("/api/scan", files={"file": ("photo.jpg", b"hello, I am text" * 10, "image/jpeg")})
    assert res.status_code == 415
    assert "error" in res.json()


def test_scan_requires_file(client):
    assert client.post("/api/scan", data={"other": "x"}).status_code == 400
    assert client.post("/api/scan", content=b"raw").status_code == 400


def test_temp_file_deleted(client, monkeypatch):
    seen: list[Path] = []

    def fake_scan(path, filename, mime):
        seen.append(Path(path))
        assert Path(path).exists()
        raise RuntimeError("boom")

    monkeypatch.setattr("app.scan.scan_file", fake_scan)
    before = temp_files()
    res = client.post("/api/scan", files={"file": ("x.png", png_bytes(), "image/png")})
    assert res.status_code == 500
    assert seen and not seen[0].exists()
    assert temp_files() == before


@pytest.mark.parametrize(
    "files",
    [
        {"file": ("x.png", png_bytes(), "image/png")},
        {"file": ("x.jpg", b"not media at all", "image/jpeg")},
    ],
)
def test_temp_file_deleted_on_success_and_reject(client, files):
    before = temp_files()
    client.post("/api/scan", files=files)
    assert temp_files() == before


def test_scan_rate_limit_429(make_client):
    client = make_client(scan_rate=2)
    for _ in range(2):
        assert client.post("/api/scan", files={"file": ("x.png", png_bytes(), "image/png")}).status_code == 200
    res = client.post("/api/scan", files={"file": ("x.png", png_bytes(), "image/png")})
    assert res.status_code == 429


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff\xe0" + b"\0" * 20, ("image", "image/jpeg")),
        (b"GIF89a" + b"\0" * 20, ("image", "image/gif")),
        (b"RIFF\0\0\0\0WEBPVP8 ", ("image", "image/webp")),
        (b"\0\0\0\x18ftypisom\0\0\0\0isomavc1", ("video", "video/mp4")),
        (b"\0\0\0\x14ftypqt  \0\0\0\0qt  ", ("video", "video/quicktime")),
        (b"\0\0\0\x18ftypheic\0\0\0\0mif1heic", ("image", "image/heic")),
        (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm", ("video", "video/webm")),
        (b"\0\0\0\x18ftypM4A \0\0\0\0M4A isom", None),
        (b"%PDF-1.7", None),
        (b"<svg xmlns=", None),
    ],
)
def test_sniff(head, expected):
    assert sniff(head) == expected


def test_scan_oversize_413_while_streaming(make_client):
    client = make_client(max_upload_mb=1)
    boundary = "btfboundary"

    def body():
        yield (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"v.mp4\"\r\n"
            "Content-Type: video/mp4\r\n\r\n"
        ).encode()
        for _ in range(64):
            yield b"\0" * 64 * 1024
        yield f"\r\n--{boundary}--\r\n".encode()

    res = client.post(
        "/api/scan",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert res.status_code == 413
