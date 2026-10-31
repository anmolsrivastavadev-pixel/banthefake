"""Detector model tests with the real pinned weights. Need `uv sync --extra ml`; set HF_HOME to the cache.

Measured scores at the pinned revisions (H = haywoodsloan, C = CommunityForensics):
  ai_dalle_mars 0.9999/0.9941, ai_dalle_cat_q70_640 0.9999/0.9978, ai_face_stylegan 0.9845/1.0000,
  ai_midjourney_city 0.9994/0.6135 -> all "likely";
  real_cat_camera_exif 0.0095/0.0001, real_cat_q70_640 0.0081/0.0008, real_bridge 0.0012/0.0001,
  real_face_truman 0.0002/0.0001 (low-colour), pixel10_verified 0.0020/0.0113, CA 0.0113/0.0001,
  C 0.9461/0.0111 -> all "none".
"""

import pytest
from PIL import Image

from app.scan import model
from tests.conftest import FIXTURES

pytestmark = pytest.mark.ml

AI = ["ai_dalle_mars.jpg", "ai_dalle_cat_q70_640.jpg", "ai_face_stylegan.jpg", "ai_midjourney_city.jpg"]
REAL = [
    "real_cat_camera_exif.jpg", "real_cat_q70_640.jpg", "real_bridge.jpg", "real_face_truman.jpg",
    "pixel10_verified.jpg", "C.jpg", "CA.jpg",
]


def scan(client, name):
    res = client.post("/api/scan", files={"file": (name, (FIXTURES / name).read_bytes())})
    assert res.status_code == 200, res.text
    return res.json()


def test_default_model_revisions_are_pinned(monkeypatch):
    for var in ("BTF_MODEL_ID", "BTF_MODEL_REVISION", "BTF_MODEL2_ID", "BTF_MODEL2_REVISION"):
        monkeypatch.delenv(var, raising=False)
    assert model.model_revision() == model.PRIMARY_REVISION
    assert model.SECONDARY.revision() == model.SECONDARY_REVISION


def test_custom_model_requires_explicit_revision(monkeypatch):
    monkeypatch.setenv("BTF_MODEL_ID", "local/custom-model")
    monkeypatch.setenv("BTF_MODEL2_ID", "local/custom-model-2")
    monkeypatch.delenv("BTF_MODEL_REVISION", raising=False)
    monkeypatch.delenv("BTF_MODEL2_REVISION", raising=False)
    assert model.model_revision() is None and model.SECONDARY.revision() is None
    monkeypatch.setenv("BTF_MODEL_REVISION", "reviewed-commit")
    monkeypatch.setenv("BTF_MODEL2_REVISION", "reviewed-commit-2")
    assert model.model_revision() == "reviewed-commit"
    assert model.SECONDARY.revision() == "reviewed-commit-2"


@pytest.mark.parametrize("name", AI)
def test_ai_fixtures_are_likely_ai(client, name):
    body = scan(client, name)
    assert body["verdict"] == "ai" and body["verdict_title"] == "Likely AI-generated", body["model"]
    m = body["model"]
    assert m["decision"] == "likely" and m["frames_checked"] == 1
    assert [s["name"] for s in m["models"]] == [model.PRIMARY_ID, "CommunityForensics-DeepfakeDet-ViT"]
    h, c = (s["ai_probability"] for s in m["models"])
    assert m["ai_probability"] == h >= model.H_LIKELY and c >= model.C_AGREE
    assert "both of our detector models agree" in body["verdict_detail"]
    assert "Detectors make mistakes" in body["verdict_detail"]


@pytest.mark.parametrize("name", REAL)
def test_real_fixtures_are_never_likely_ai(client, name):
    body = scan(client, name)
    assert body["verdict"] != "ai", body["model"]
    assert body["model"] is not None and body["model"]["decision"] != "likely"


def test_batch_matches_single():
    imgs = [Image.open(FIXTURES / n).convert("RGB") for n in ("ai_dalle_mars.jpg", "real_bridge.jpg")]
    batch = model.predict(imgs)
    singles = [model.predict([i]) for i in imgs]
    assert batch.h == pytest.approx([s.h[0] for s in singles], abs=1e-4)
    assert batch.c == pytest.approx([s.c[0] for s in singles], abs=1e-4)
    assert model.status() == "loaded"


def test_off_returns_none(monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "off")
    assert model.predict([Image.new("RGB", (32, 32))]) is None
    assert model.status() == "off"


def test_ensemble_off_runs_secondary_alone_with_previous_thresholds(client, monkeypatch):
    monkeypatch.setenv("BTF_MODEL_ENSEMBLE", "off")
    body = scan(client, "ai_dalle_mars.jpg")
    m = body["model"]
    assert m["name"] == "CommunityForensics-DeepfakeDet-ViT"
    assert [s["name"] for s in m["models"]] == ["CommunityForensics-DeepfakeDet-ViT"]
    assert m["ai_probability"] >= model.C_LIKELY_ALONE and body["verdict"] == "ai"
    # the Midjourney fixture is only "possible" for this model on its own (C = 0.61)
    body = scan(client, "ai_midjourney_city.jpg")
    assert body["verdict_title"] == "Possible signs of AI generation"


def test_primary_load_failure_degrades_to_possible(client, monkeypatch):
    broken = model._Detector("/nonexistent/detector", "", "BTF_TEST_UNUSED_ID", "BTF_TEST_UNUSED_REV")
    monkeypatch.setattr(model, "PRIMARY", broken)
    body = scan(client, "ai_dalle_mars.jpg")
    assert broken.failed
    assert body["verdict"] == "unknown" and body["verdict_title"] == "Possible signs of AI generation"
    m = body["model"]
    assert m["decision"] == "possible" and [s["name"] for s in m["models"]] == ["CommunityForensics-DeepfakeDet-ViT"]
    assert any("could not be loaded" in s["text"] for s in body["signals"] if s["kind"] == "info")
    assert model.status() == "degraded"
    assert client.get("/api/health").json()["model"] == "degraded"


def test_e2e_low_score_never_says_real(client):
    body = scan(client, "real_bridge.jpg")
    assert body["verdict"] == "unknown" and body["model"]["decision"] == "none"
    msg = next(s["text"] for s in body["signals"] if s["kind"] == "model")
    assert "does not mean it is real" in msg and "1%" in msg  # never shown as 0%


def test_e2e_marker_beats_low_model_score(client):
    from tests import media

    body = client.post("/api/scan", files={"file": ("sd.png", media.png({"parameters": media.SD_PARAMETERS}))}).json()
    assert body["verdict"] == "ai" and body["model"] is not None


def test_e2e_video_frames(client):
    body = scan(client, "test_clip.mp4")
    m = body["model"]
    assert m["frames_checked"] == 8 and len(m["frame_probabilities"]) == 8
    assert all(len(s["frame_probabilities"]) == 8 for s in m["models"]) and len(m["models"]) == 2
    # The clip cycles real_cat, ai_dalle_cat, real_bridge: 3 of 8 sampled frames are the AI image, which is a
    # majority of the most suspicious half, so the partly synthetic clip is "likely" (not diluted by real frames).
    assert m["decision"] == "likely" and body["verdict"] == "ai"
    assert "8 sampled frames" in next(s["text"] for s in body["signals"] if s["kind"] == "model")
    assert "3 of 8 rated likely AI" in next(s["text"] for s in body["signals"] if s["kind"] == "model")


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
    assert body["model"] is not None and body["model"]["decision"] == "likely"
    assert body["verdict"] == "ai"
