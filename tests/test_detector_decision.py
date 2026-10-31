"""The two-model agreement rule and how scan reports use it. The detectors are faked (no ML deps needed)."""

import io
import re

import pytest
from PIL import Image

from app.scan import model
from tests import media
from tests.conftest import FIXTURES

H_NAME, C_NAME = model.PRIMARY_ID, "CommunityForensics-DeepfakeDet-ViT"


@pytest.mark.parametrize(
    ("h", "c", "expected"),
    [
        (0.999, 0.9, "likely"),  # both agree
        (0.98, 0.05, "likely"),  # thresholds are inclusive
        (0.999, 0.049, "possible"),  # only H: the secondary model rejects it
        (0.979, 0.99, "possible"),  # only C
        (0.97, 0.3, "none"),
        (0.2, 0.49, "none"),
    ],
)
def test_agreement_rule(h, c, expected):
    assert model.decide(h, c) == expected


@pytest.mark.parametrize(
    ("h", "c", "expected"),
    [(0.9999, None, "possible"), (0.5, None, "none"), (None, 0.9999, "possible"), (None, 0.4, "none")],
)
def test_one_model_unavailable_never_gives_likely(h, c, expected):
    assert model.decide(h, c) == expected


@pytest.mark.parametrize(("c", "expected"), [(0.85, "likely"), (0.6, "possible"), (0.49, "none"), (None, "none")])
def test_ensemble_off_uses_secondary_thresholds(c, expected):
    assert model.decide(0.9999, c, agreement=False) == expected


@pytest.mark.parametrize(
    ("tiers", "expected"),
    [
        (["likely"], "likely"),
        (["likely", "none"], "likely"),  # two frames: the most suspicious one decides, as before
        (["likely", "none", "none"], "none"),  # one odd frame of three cannot decide
        (["likely", "likely", "none"], "likely"),
        (["likely"] * 3 + ["none"] * 5, "likely"),  # a partly synthetic clip is not diluted
        (["likely"] * 2 + ["none"] * 6, "none"),
        (["likely"] * 2 + ["possible"] + ["none"] * 5, "possible"),
    ],
)
def test_video_aggregation(tiers, expected):
    assert model.aggregate(tiers) == expected


def test_prediction_requires_matching_lengths():
    with pytest.raises(ValueError):
        model.Prediction(h=[0.1], c=[0.1, 0.2])
    with pytest.raises(ValueError):
        model.Prediction(h=None, c=None)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "auto")

    def use(h, c=-1.0):
        monkeypatch.setattr(model, "predict", media.fake_predict(h, c))

    return use


def scan(client, name, data):
    res = client.post("/api/scan", files={"file": (name, data)})
    assert res.status_code == 200, res.text
    return res.json()


def model_texts(body):
    return [s["text"] for s in body["signals"] if s["kind"] == "model"]


def test_both_agree_is_likely_ai(client, fake):
    fake(0.995, 0.4)
    body = scan(client, "x.png", media.png())
    assert body["verdict"] == "ai" and body["verdict_title"] == "Likely AI-generated"
    assert "both of our detector models agree" in body["verdict_detail"]
    assert "Both detector models agree" in model_texts(body)[0]


def test_model_block_shape(client, fake):
    fake(0.995, 0.4)
    m = scan(client, "x.png", media.png())["model"]
    assert set(m) == {"name", "ai_probability", "decision", "models", "frames_checked"}
    assert m["name"] == f"{H_NAME} + {C_NAME} (agreement)"
    assert m["models"] == [{"name": H_NAME, "ai_probability": 0.995}, {"name": C_NAME, "ai_probability": 0.4}]
    assert m["ai_probability"] == 0.995  # the primary model's probability
    assert m["decision"] == "likely" and m["frames_checked"] == 1


def test_only_primary_high_is_possible(client, fake):
    fake(0.999, 0.01)
    body = scan(client, "x.png", media.png())
    assert body["verdict"] == "unknown" and body["verdict_title"] == "Possible signs of AI generation"
    assert body["model"]["decision"] == "possible"
    assert "do not both agree" in model_texts(body)[0]


def test_only_secondary_high_is_possible(client, fake):
    fake(0.3, 0.99)
    body = scan(client, "x.png", media.png())
    assert body["verdict_title"] == "Possible signs of AI generation"
    assert body["model"]["ai_probability"] == 0.3


def test_neither_is_no_signal(client, fake):
    fake(0.1, 0.2)
    body = scan(client, "x.png", media.png())
    assert body["verdict"] == "unknown" and body["model"]["decision"] == "none"
    assert "no strong AI signal" in model_texts(body)[0]


@pytest.mark.parametrize(("h", "c", "missing"), [(0.9999, None, C_NAME), (None, 0.9999, H_NAME)])
def test_one_model_unavailable_is_capped_at_possible(client, fake, h, c, missing):
    fake(h, c)
    body = scan(client, "x.png", media.png())
    assert body["verdict"] == "unknown" and body["verdict_title"] == "Possible signs of AI generation"
    m = body["model"]
    assert m["decision"] == "possible" and len(m["models"]) == 1 and m["name"] != missing
    assert m["ai_probability"] == 0.9999  # the model that ran
    info = [s["text"] for s in body["signals"] if s["kind"] == "info"]
    assert any(missing in t and "could not be loaded" in t for t in info)


def test_low_colour_never_drives_an_ai_verdict(client, fake):
    fake(0.999, 0.999)
    buf = io.BytesIO()
    Image.new("L", (96, 64), 128).save(buf, "PNG")
    body = scan(client, "grey.png", buf.getvalue())
    assert body["verdict"] == "unknown" and body["verdict_title"] == "Detector unreliable for this image"
    assert body["model"]["decision"] == "likely"  # the raw decision is still reported
    assert body["model"]["ai_probability"] == 0.999


def test_video_decides_per_frame(client, monkeypatch):
    """Per-frame rule: H high on every frame, but C agrees on only 2 of 8 frames -> not "likely"."""
    monkeypatch.setenv("BTF_MODEL", "auto")
    c_frames = [0.9, 0.9, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01]

    def predict(images):
        n = len(images)
        return model.Prediction(h=[0.999] * n, c=(c_frames * 2)[:n])

    monkeypatch.setattr(model, "predict", predict)
    body = scan(client, "v.mp4", (FIXTURES / "test_clip.mp4").read_bytes())
    m = body["model"]
    assert m["frames_checked"] == 8 and m["decision"] == "possible"
    assert body["verdict_title"] == "Possible signs of AI generation"
    assert len(m["frame_probabilities"]) == 8 and all(len(s["frame_probabilities"]) == 8 for s in m["models"])
    assert m["models"][1]["ai_probability"] == pytest.approx((0.9 + 0.9 + 0.01 + 0.01) / 4, abs=1e-4)
    assert "2 of 8 sampled frames were individually rated likely AI-generated" in model_texts(body)[0]


def test_frontend_shows_both_scores_and_agreement_note(client):
    js = client.get("/app.js").text
    assert "m.models" in js and "both" in js.lower()
    assert re.search(r"likely.{0,80}both.{0,40}agree", js, re.IGNORECASE | re.DOTALL)
    # strict CSP: no inline handlers or HTML injection
    assert "innerHTML" not in js and not re.search(r"\son[a-z]+\s*=", js)
    assert "m.decision" in js  # the reading comes from the server's combined decision, not a local threshold
