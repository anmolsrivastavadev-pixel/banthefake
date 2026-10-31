"""Detector model tests. Need `uv sync --extra ml`; weights come from the HF cache (set HF_HOME)."""

import pytest
from PIL import Image

from app.scan import model
from tests.conftest import FIXTURES

pytestmark = pytest.mark.ml

AI = ["ai_dalle_mars.jpg", "ai_dalle_cat_q70_640.jpg", "ai_face_stylegan.jpg"]
REAL = ["real_bridge.jpg", "real_cat_q70_640.jpg", "real_face_truman.jpg", "real_cat_camera_exif.jpg"]


def test_default_model_revision_is_pinned(monkeypatch):
    monkeypatch.delenv("BTF_MODEL_ID", raising=False)
    monkeypatch.delenv("BTF_MODEL_REVISION", raising=False)
    assert model.model_revision() == model.DEFAULT_MODEL_REVISION


def test_custom_model_requires_explicit_revision(monkeypatch):
    monkeypatch.setenv("BTF_MODEL_ID", "local/custom-model")
    monkeypatch.delenv("BTF_MODEL_REVISION", raising=False)
    assert model.model_revision() is None
    monkeypatch.setenv("BTF_MODEL_REVISION", "reviewed-commit")
    assert model.model_revision() == "reviewed-commit"


def score(name):
    from app.scan import exif

    return model.predict([exif.load_for_model(FIXTURES / name)])[0]


@pytest.mark.parametrize("name", AI)
def test_ai_images_score_high(name):
    assert score(name) >= model.LIKELY


@pytest.mark.parametrize("name", REAL)
def test_real_images_score_low(name):
    assert score(name) < 0.1


def test_batch_matches_single():
    imgs = [Image.open(FIXTURES / n).convert("RGB") for n in ("ai_dalle_mars.jpg", "real_bridge.jpg")]
    batch = model.predict(imgs)
    assert batch == pytest.approx([model.predict([i])[0] for i in imgs], abs=1e-4)
    assert model.status() == "loaded"


def test_off_returns_none(monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "off")
    assert model.predict([Image.new("RGB", (32, 32))]) is None
    assert model.status() == "off"


def test_e2e_likely_ai(client):
    body = client.post("/api/scan", files={"file": ("a.jpg", (FIXTURES / "ai_dalle_mars.jpg").read_bytes())}).json()
    assert body["verdict"] == "ai" and body["verdict_title"] == "Likely AI-generated"
    assert body["model"]["frames_checked"] == 1 and body["model"]["ai_probability"] >= model.LIKELY
    assert "Detectors make mistakes" in body["verdict_detail"]


def test_e2e_possible_signs(client):
    body = client.post("/api/scan", files={"file": ("m.jpg", (FIXTURES / "ai_midjourney_city.jpg").read_bytes())}).json()
    assert body["verdict"] == "unknown" and body["verdict_title"] == "Possible signs of AI generation"
    assert model.POSSIBLE <= body["model"]["ai_probability"] < model.LIKELY


def test_e2e_low_score_never_says_real(client):
    body = client.post("/api/scan", files={"file": ("b.jpg", (FIXTURES / "real_bridge.jpg").read_bytes())}).json()
    assert body["verdict"] == "unknown" and body["model"]["ai_probability"] < model.POSSIBLE
    msg = next(s["text"] for s in body["signals"] if s["kind"] == "model")
    assert "does not mean it is real" in msg and "1%" in msg  # never shown as 0%


def test_e2e_marker_beats_low_model_score(client):
    from tests import media

    body = client.post("/api/scan", files={"file": ("sd.png", media.png({"parameters": media.SD_PARAMETERS}))}).json()
    assert body["verdict"] == "ai" and body["model"] is not None


def test_e2e_video_frames(client):
    body = client.post("/api/scan", files={"file": ("v.mp4", (FIXTURES / "test_clip.mp4").read_bytes())}).json()
    m = body["model"]
    assert m["frames_checked"] == 8 and len(m["frame_probabilities"]) == 8
    assert 0 <= m["ai_probability"] <= 1
    assert "8 sampled frames" in next(s["text"] for s in body["signals"] if s["kind"] == "model")


@pytest.mark.parametrize("damage", ["truncate2", "truncate1k", "ztxt_png"])
def test_e2e_damaged_ai_image_still_detected(client, damage):
    """Regression: damaged files used to skip the detector silently (verdict "unknown", no model block)."""
    import io

    from tests import media

    data = (FIXTURES / "ai_face_stylegan.jpg").read_bytes()
    if damage == "truncate2":
        data = data[:-2]
    elif damage == "truncate1k":
        data = data[:-1000]
    else:
        buf = io.BytesIO()
        Image.open(FIXTURES / "ai_face_stylegan.jpg").save(buf, "PNG")
        data = media.insert_png_chunk(buf.getvalue(), media.ztxt_bomb())
    body = client.post("/api/scan", files={"file": ("x", data)}).json()
    assert body["model"] is not None and body["model"]["ai_probability"] >= model.LIKELY
    assert body["verdict"] == "ai"
