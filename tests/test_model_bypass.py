"""Issue: damaged or oversized images silently skipped the AI detector, so they looked clean.

The detector is replaced by a fake here (no ML deps needed); see test_model.py for the real model.
"""

import pytest
from PIL import ImageFile

from app.scan import exif, model
from tests import media

COULD_NOT = "AI detector could not analyze this file"
NO_METADATA = "No metadata at all"


@pytest.fixture
def detector(monkeypatch):
    """A fake detector that records what it was given."""
    calls: list[list] = []

    def predict(images):
        calls.append(images)
        return [0.97 for _ in images]

    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "predict", predict)
    return calls


def scan(client, name, data):
    res = client.post("/api/scan", files={"file": (name, data, "application/octet-stream")})
    assert res.status_code == 200, res.text
    return res.json()


def texts(body):
    return " | ".join(s["text"] for s in body["signals"])


@pytest.mark.parametrize("cut", [2, 1000, 0.66])
def test_truncated_jpeg_still_analyzed(client, detector, cut):
    data = media.photo_jpeg()
    n = int(len(data) * cut) if isinstance(cut, float) else cut
    body = scan(client, "t.jpg", data[: len(data) - n])
    assert detector and body["model"] is not None, texts(body)
    assert body["model"]["ai_probability"] == pytest.approx(0.97)
    assert "damaged or truncated" in texts(body)
    assert NO_METADATA not in texts(body)


def test_truncated_jpeg_does_not_flip_global_pillow_flag(tmp_path):
    p = tmp_path / "t.jpg"
    p.write_bytes(media.photo_jpeg()[:-500])
    assert exif.decode_for_model(p).image.size == (256, 192)
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False


def test_truncated_png_pixel_data_still_analyzed(client, detector):
    data = media.photo_png()
    idat = data.index(b"IDAT")
    body = scan(client, "t.png", data[: idat + 200])
    assert body["model"] is not None, texts(body)
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False


@pytest.mark.parametrize("after_image", [False, True])
def test_png_ztxt_bomb_still_analyzed(client, detector, after_image):
    data = media.insert_png_chunk(media.photo_png(), media.ztxt_bomb(), after_image)
    body = scan(client, "z.png", data)
    assert body["model"] is not None, texts(body)
    assert body["file"]["width"] == 256
    assert NO_METADATA not in texts(body)


def test_png_text_after_image_data_is_read(client):
    import zlib

    chunk = media.png_chunk(b"zTXt", b"parameters\0\0" + zlib.compress(media.SD_PARAMETERS.encode()))
    body = scan(client, "sd.png", media.insert_png_chunk(media.photo_png(), chunk, after_image=True))
    assert body["verdict"] == "ai"
    assert any(s.get("field") == "PNG text “parameters”" for s in body["signals"])


def test_png_text_reader_is_bounded(tmp_path):
    p = tmp_path / "z.png"
    p.write_bytes(media.insert_png_chunk(media.photo_png(), media.ztxt_bomb(inflated=50 * 1024 * 1024)))
    texts_ = exif._png_text_chunks(p)
    assert len(texts_["Comment"]) == exif.MAX_TEXT


def test_png_over_decode_limit_gets_explicit_signal(client, detector, monkeypatch):
    monkeypatch.setattr(exif, "MAX_DECODE_PIXELS", 10_000)
    body = scan(client, "big.png", media.photo_png())  # 256x192 = 49k pixels
    assert not detector and body["model"] is None
    msg = next(s["text"] for s in body["signals"] if COULD_NOT in s["text"])
    assert "256×192" in msg
    assert NO_METADATA not in texts(body)


def test_jpeg_over_decode_limit_is_downscaled(client, detector, monkeypatch):
    monkeypatch.setattr(exif, "MAX_DECODE_PIXELS", 20_000)
    body = scan(client, "big.jpg", media.photo_jpeg())  # 49k pixels -> decoded at 1/2 scale
    assert body["model"] is not None and COULD_NOT not in texts(body)


@pytest.mark.parametrize(
    ("w", "h", "expected"),
    [(1000, 800, 1), (4096, 3072, 2), (16384, 1000, 8), (9000, 9000, 4), (12000, 10000, 4), (60000, 2000, 8)],
)
def test_jpeg_draft_scale(w, h, expected):
    scale = exif._jpeg_scale(w, h, 2048)
    assert scale == expected
    assert -(-w // scale) * -(-h // scale) <= exif.MAX_DECODE_PIXELS


def test_jpeg_draft_scale_respects_pixel_limit():
    # even when the caller wants full resolution, a 48 MP JPEG is decoded at half scale (12 MP)
    assert exif._jpeg_scale(8000, 6000, 100_000) == 2


def test_decode_limit_default_is_25mp():
    assert exif.MAX_DECODE_PIXELS == 25_000_000


def test_undecodable_image_gets_explicit_signal(client, detector):
    body = scan(client, "bad.jpg", b"\xff\xd8\xff\xe0" + b"garbage" * 50)
    assert body["model"] is None and COULD_NOT in texts(body)


def test_model_unavailable_gets_explicit_signal(client, monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "predict", lambda images: None)
    body = scan(client, "plain.png", media.png())
    assert body["model"] is None
    assert "AI detector could not analyze this file (the detector is not available on this server)." in texts(body)
    assert NO_METADATA not in texts(body)


def test_model_off_has_no_detector_signal(client):
    body = scan(client, "plain.png", media.png())
    assert COULD_NOT not in texts(body) and NO_METADATA in texts(body)
