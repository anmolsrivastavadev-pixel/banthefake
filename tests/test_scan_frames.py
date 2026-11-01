"""POST /api/scan-frames: frames captured by the browser extension from a video the user is playing."""

import io
import re
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from app import main, scan
from app.scan import isolate, model
from tests import media

EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _frames(n: int, data: bytes | None = None, name: str = "frame.jpg", ctype: str = "image/jpeg"):
    return [("frames", (f"{i}-{name}", data or media.photo_jpeg(), ctype)) for i in range(n)]


def _post(client, files, origin: str | None = None):
    headers = {"Origin": origin} if origin else {}
    return client.post("/api/scan-frames", files=files, headers=headers)


@pytest.fixture
def detector(monkeypatch):
    """Turn the detector on with deterministic per-frame scores (h_frames/c_frames, cycled)."""
    monkeypatch.setenv("BTF_MODEL", "auto")

    def use(h_frames: list[float], c_frames: list[float] | None = None):
        monkeypatch.setattr(model, "predict", media.fake_frame_predict(h_frames, c_frames or h_frames))

    return use


def _model_text(body) -> str:
    return " ".join(s["text"] for s in body["signals"] if s["kind"] == "model")


def test_happy_path_aggregates_like_a_video(client, detector, monkeypatch):
    calls = []
    real_parse = isolate.parse

    def counting_parse(*args, **kwargs):
        calls.append(args[2])
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(isolate, "parse", counting_parse)
    detector([0.999] * 5 + [0.01] * 3, [0.9] * 5 + [0.01] * 3)  # 5 of 8 frames "likely"
    res = _post(client, _frames(8))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["verdict"] == "ai" and body["verdict_title"] == "Likely AI-generated"
    assert body["file"]["type"] == "video_frames" and body["file"]["frames_checked"] == 8
    assert body["file"]["mime"] == "image/jpeg" and body["file"]["width"] == 256
    assert body["c2pa"] is None
    m = body["model"]
    assert m["frames_checked"] == 8 and m["decision"] == "likely" and m["frames_likely"] == 5
    assert len(m["frame_probabilities"]) == 8
    # every frame went through the existing isolated image parser, one job per frame
    assert calls == ["image/jpeg"] * 8
    texts = " ".join(s["text"] for s in body["signals"])
    assert "captured from a video playing in your browser" in texts
    assert "audio is not analysed" in texts and "re-compression" in texts
    assert scan.NO_PROOF in texts
    assert "Metadata, content credentials (C2PA) and AI-generator markers were not checked" in texts
    assert "duration is unknown" not in texts  # captured frames have no file duration to report


def test_one_likely_frame_makes_the_clip_at_least_possible(client, detector):
    detector([0.999] + [0.01] * 7, [0.9] + [0.01] * 7)
    body = _post(client, _frames(8)).json()
    assert body["model"]["decision"] == "possible"
    assert body["verdict"] == "unknown" and body["verdict_title"] == "Possible signs of AI generation"
    assert "1 of 8 sampled frames were individually rated likely AI-generated" in _model_text(body)
    assert "real" not in body["verdict_title"].lower()


def test_clean_frames_never_read_as_real(client, detector):
    detector([0.01])
    body = _post(client, _frames(4)).json()
    assert body["model"]["decision"] == "none"
    assert body["verdict_title"] == "No conclusive signals found"
    assert "does NOT mean it is real" in body["verdict_detail"]
    assert "does not mean it is real" in _model_text(body)


def test_paused_single_frame_is_decided_as_one_image(client, detector):
    detector([0.999], [0.9])
    body = _post(client, _frames(1)).json()
    assert body["file"]["frames_checked"] == 1 and body["model"]["frames_checked"] == 1
    assert body["model"]["decision"] == "likely"
    assert any(s["text"].startswith("This frame was captured") for s in body["signals"])


def test_metadata_in_frames_is_ignored(client, detector):
    detector([0.01])
    ai_png = media.png({"parameters": media.SD_PARAMETERS})
    body = _post(client, _frames(2, ai_png, "f.png", "image/png")).json()
    assert body["verdict"] != "ai"
    assert not any(s["kind"] in ("ai", "edit", "camera", "c2pa") for s in body["signals"])
    assert body["file"]["mime"] == "image/png"


def test_png_and_webp_frames_are_accepted_with_model_off(client):
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, "WEBP")
    files = _frames(1, media.photo_png(), "a.png", "image/png") + _frames(1, buf.getvalue(), "b.webp", "image/webp")
    res = _post(client, files)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["model"] is None and body["file"]["frames_checked"] == 2 and body["file"]["mime"] == "image/*"


def test_too_many_frames_is_413(client):
    res = _post(client, _frames(main.MAX_FRAMES + 1))
    assert res.status_code == 413
    assert "16" in res.json()["error"]


def test_oversized_frame_is_413(client):
    big = media.photo_jpeg() + b"\0" * main.MAX_FRAME_BYTES
    assert _post(client, _frames(1, big)).status_code == 413


def test_total_size_cap_is_413(client):
    per = main.MAX_FRAME_BYTES - 1024
    n = main.MAX_FRAMES_TOTAL_BYTES // per + 1
    data = media.photo_jpeg() + b"\0" * (per - len(media.photo_jpeg()))
    assert _post(client, _frames(n, data)).status_code == 413


def test_total_is_also_capped_by_max_upload(make_client):
    client = make_client(max_upload_mb=1)
    data = media.photo_jpeg() + b"\0" * (600 * 1024)
    assert _post(client, _frames(2, data)).status_code == 413


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"<html>not an image</html>", "x.html"),
        (b"GIF89a" + b"\0" * 64, "x.gif"),  # an image, but not a captured-frame format
        ((Path(__file__).parent / "fixtures" / "test_clip.mp4").read_bytes()[:4096], "x.mp4"),
    ],
)
def test_non_frame_part_is_415(client, data, name):
    files = _frames(1) + [("frames", (name, data, "image/jpeg"))]
    res = _post(client, files)
    assert res.status_code == 415
    assert "JPEG, PNG or WEBP" in res.json()["error"]


def test_no_frames_or_empty_frame_is_400(client):
    assert client.post("/api/scan-frames", files={"file": ("x.jpg", media.jpeg(), "image/jpeg")}).status_code == 400
    res = client.post("/api/scan-frames", files=[("frames", ("e.jpg", b"", "image/jpeg"))])
    assert res.status_code == 400
    assert client.post("/api/scan-frames", data={"x": "1"}).status_code == 400


def test_undecodable_frames_are_skipped_or_rejected(client):
    broken = b"\xff\xd8\xff\xe0" + b"\0" * 200
    assert _post(client, _frames(2, broken)).status_code == 415
    res = _post(client, _frames(1) + _frames(1, broken))
    assert res.status_code == 200
    body = res.json()
    assert body["file"]["frames_checked"] == 1
    assert any("1 of 2 captured frames could not be decoded" in s["text"] for s in body["signals"])


def test_rate_limited_like_scan(make_client):
    client = make_client(scan_rate=2)
    assert _post(client, _frames(1)).status_code == 200
    assert client.post("/api/scan", files={"file": ("x.jpg", media.jpeg(), "image/jpeg")}).status_code == 200
    res = _post(client, _frames(1))
    assert res.status_code == 429  # one shared per-IP scan budget for both endpoints


def test_busy_when_no_scan_slot(make_client, monkeypatch):
    client = make_client(scan_concurrency=1, scan_wait_s=0.01)

    async def never(self, timeout):
        return False

    monkeypatch.setattr(main.ScanSlots, "acquire", never)
    res = _post(client, _frames(1))
    assert res.status_code == 503 and res.json()["error"] == "busy"


def test_scan_timeout_is_504(client, monkeypatch):
    def slow(parts):
        raise scan.ScanTimeout("deadline")

    monkeypatch.setattr(scan, "scan_frames", slow)
    assert _post(client, _frames(1)).status_code == 504


def test_cors_only_when_configured(client, make_client):
    res = _post(client, _frames(1), EXT)
    assert res.status_code == 200 and "access-control-allow-origin" not in res.headers
    pre = client.options("/api/scan-frames", headers={"Origin": EXT, "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 405

    cors = make_client(cors_origins=(EXT,))
    res = _post(cors, _frames(1), EXT)
    assert res.headers["access-control-allow-origin"] == EXT
    assert "access-control-allow-credentials" not in res.headers
    pre = cors.options("/api/scan-frames", headers={"Origin": EXT, "Access-Control-Request-Method": "POST"})
    assert pre.status_code == 204 and pre.headers["access-control-allow-methods"] == "POST"
    other = _post(cors, _frames(1), "https://evil.example")
    assert "access-control-allow-origin" not in other.headers


def test_temp_files_are_deleted(client, detector, monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    detector([0.5])

    def leftovers():
        return [p for p in tmp_path.iterdir() if p.name.startswith("btf-frames-")]

    assert _post(client, _frames(3)).status_code == 200
    assert _post(client, _frames(1, b"nope")).status_code == 415
    assert _post(client, _frames(main.MAX_FRAMES + 1)).status_code == 413

    def boom(parts):
        raise RuntimeError("scanner crashed")

    monkeypatch.setattr(scan, "scan_frames", boom)
    assert _post(client, _frames(1)).status_code == 500
    assert leftovers() == []


def test_server_never_fetches_urls():
    """Static guarantee: nothing in app/ can download a URL (no YouTube/TikTok fetching, no SSRF surface)."""
    forbidden = re.compile(
        r"^\s*(import|from)\s+(urllib\.request|urllib3|requests|httpx|aiohttp|http\.client|pycurl|yt_dlp|"
        r"youtube_dl|websockets?|ftplib|smtplib|socket)\b|urlopen\(|urlretrieve\(|create_connection\(",
        re.MULTILINE,
    )
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        offenders += [f"{path.name}: {m.group(0).strip()}" for m in forbidden.finditer(path.read_text())]
    assert offenders == []
    # The frame endpoint accepts uploaded bytes only: no URL field is read anywhere.
    source = (APP_DIR / "main.py").read_text()
    route = source[source.index('@app.post("/api/scan-frames")'):source.index('@app.api_route("/privacy.html"')]
    assert "url" not in route.lower()
