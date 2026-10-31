"""Regression tests for the third review round: C2PA trust, ingredient/placed actions, partly-AI video, NaN
scores, low-colour wording, the trust-anchor override, unverified claims and the degraded health state."""

import io
import logging
import math
import re
import shutil
from types import SimpleNamespace

import pytest
from PIL import Image

from app.scan import c2pa_check, model
from tests import media
from tests.conftest import FIXTURES

PROBE = FIXTURES / "probe_c2pa"
PROBE_CA = PROBE / "ca.pem"
needs_c2pa = pytest.mark.skipif(not c2pa_check.AVAILABLE, reason="c2pa-python not installed")


def post(client, name, data):
    res = client.post("/api/scan", files={"file": (name, data)})
    assert res.status_code == 200, res.text
    return res.json()


def texts(body, kind=None):
    return [s["text"] for s in body["signals"] if kind is None or s["kind"] == kind]


@pytest.fixture
def fresh_c2pa(monkeypatch):
    """Rebuild the cached c2pa context for this test, and again afterwards."""
    monkeypatch.setattr(c2pa_check, "_context", None)
    monkeypatch.delenv("BTF_C2PA_TRUST", raising=False)
    monkeypatch.delenv("BTF_C2PA_TRUST_ANCHORS", raising=False)
    monkeypatch.delenv("BTF_C2PA_TRUST_ANCHORS_MODE", raising=False)
    yield
    c2pa_check._context = None


@pytest.fixture
def probe_trusted(fresh_c2pa, monkeypatch):
    monkeypatch.setenv("BTF_C2PA_TRUST_ANCHORS", str(PROBE_CA))


# --- 1. TSA roots must not make a manifest signer trusted ---------------------------------------------------


def _trust_dir(tmp_path, signer_extra="", tsa_extra=""):
    d = tmp_path / "trust"
    d.mkdir()
    for name, extra in (("C2PA-TRUST-LIST.pem", signer_extra), ("C2PA-TSA-TRUST-LIST.pem", tsa_extra)):
        (d / name).write_text((c2pa_check.TRUST_DIR / name).read_text() + "\n" + extra)
    return d


@needs_c2pa
def test_tsa_only_root_does_not_trust_a_signer(fresh_c2pa, monkeypatch, tmp_path):
    """The probe CA is added to the TSA list only: a manifest signed under it must stay untrusted."""
    monkeypatch.setattr(c2pa_check, "TRUST_DIR", _trust_dir(tmp_path, tsa_extra=PROBE_CA.read_text()))
    r = c2pa_check.read(PROBE / "s_capture.jpg", "image/jpeg")
    assert r is not None and r.valid and not r.trusted and r.untrusted and not r.capture
    # the official signer list still works
    pixel = c2pa_check.read(FIXTURES / "pixel10_verified.jpg", "image/jpeg")
    assert pixel.trusted and pixel.capture


@needs_c2pa
def test_signer_list_root_does_trust_a_signer(fresh_c2pa, monkeypatch, tmp_path):
    """Control for the test above: the same CA on the signer list makes the manifest trusted."""
    monkeypatch.setattr(c2pa_check, "TRUST_DIR", _trust_dir(tmp_path, signer_extra=PROBE_CA.read_text()))
    r = c2pa_check.read(PROBE / "s_capture.jpg", "image/jpeg")
    assert r is not None and r.valid and r.trusted and r.capture


# --- 3. c2pa.placed / c2pa.unknown are edits; ingredients are reported ----------------------------------------


def test_placed_and_unknown_actions_are_edits():
    assert "c2pa.placed" not in c2pa_check.NEUTRAL_ACTIONS and "c2pa.unknown" not in c2pa_check.NEUTRAL_ACTIONS
    r = c2pa_check.summarize({
        "active_manifest": "m", "validation_status": [],
        "manifests": {"m": {"assertions": [{"label": "c2pa.actions", "data": {"actions": [
            {"action": "c2pa.created"}, {"action": "c2pa.placed"}, {"action": "c2pa.unknown"}]}}]}},
    }, "Trusted")
    assert r.edit_actions == ["c2pa.placed", "c2pa.unknown"]


@needs_c2pa
def test_unknown_action_is_reported_as_edit(client, probe_trusted):
    body = post(client, "u.jpg", (PROBE / "s_capture_unknown.jpg").read_bytes())
    assert body["verdict"] == "verified_capture"
    assert body["verdict_title"] == "Verified capture with recorded edits"
    assert "recorded edits: unknown" in body["verdict_detail"]
    assert body["c2pa"]["edits"] == ["c2pa.unknown"]


@needs_c2pa
def test_placed_ingredient_is_an_edit_and_counted(client, probe_trusted):
    body = post(client, "p.jpg", (PROBE / "s_capture_placed.jpg").read_bytes())
    assert body["verdict"] == "verified_capture"
    assert body["verdict_title"] == "Verified capture with recorded edits"
    assert "placed" in body["verdict_detail"] and "include 1 ingredient " in body["verdict_detail"]
    assert body["c2pa"]["edits"] == ["c2pa.placed"] and len(body["c2pa"]["ingredients"]) == 1
    assert not body["c2pa"]["ingredient_ai"]


@needs_c2pa
def test_ingredient_declaring_ai_is_verified_ai(client, probe_trusted):
    body = post(client, "pa.jpg", (PROBE / "s_capture_placed_ai.jpg").read_bytes())
    assert body["verdict"] == "verified_ai", body["verdict_detail"]
    assert "include 1 ingredient" in body["verdict_detail"]
    assert "ingredient's own content credentials declare generative AI" in body["verdict_detail"]
    c = body["c2pa"]
    assert c["ingredient_ai"] and c["ai_generated"] and not c["capture"] and not c["claimed_capture"]
    assert c["ingredients"][0]["ai_generated"] == "true"
    assert any("ingredient whose own content credentials declare generative AI" in t for t in texts(body, "ai"))


@needs_c2pa
def test_untrusted_ingredient_ai_is_informational(client, fresh_c2pa):
    body = post(client, "pa.jpg", (PROBE / "s_capture_placed_ai.jpg").read_bytes())
    assert body["verdict"] not in ("verified_ai", "verified_capture", "ai")
    assert any("ingredients has its own content credentials declaring generative AI" in t for t in texts(body, "c2pa"))
    assert any("include 1 ingredient" in t for t in texts(body, "c2pa"))


# --- 2. a partly-AI video is never "no strong AI signal" ---------------------------------------------------


@pytest.fixture
def frames_fake(monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "auto")

    def use(h, c):
        monkeypatch.setattr(model, "predict", media.fake_frame_predict(h, c))

    return use


CLIP = (FIXTURES / "test_clip.mp4").read_bytes()  # 8 sampled frames


def test_one_likely_frame_makes_clip_possible(client, frames_fake):
    frames_fake([0.999] + [0.01] * 7, [0.9] + [0.001] * 7)
    body = post(client, "v.mp4", CLIP)
    m = body["model"]
    assert m["frames_checked"] == 8 and m["decision"] == "possible" and m["frames_likely"] == 1
    assert body["verdict_title"] == "Possible signs of AI generation"
    assert "1 of 8 sampled frames were individually rated likely AI-generated" in body["verdict_detail"]
    model_text = texts(body, "model")[0]
    assert "1 of 8 sampled frames were individually rated likely AI-generated" in model_text
    assert "do not both agree" not in model_text
    assert not any("no strong AI signal" in t for t in texts(body))


def test_possible_frames_never_say_no_strong_signal(client, frames_fake):
    frames_fake([0.999] + [0.01] * 7, [0.01] * 8)  # one frame "possible" (H alone)
    body = post(client, "v.mp4", CLIP)
    assert body["model"]["decision"] == "none" and body["model"]["frames_possible"] == 1
    assert not any("no strong AI signal" in t for t in texts(body))
    assert "1 of 8 sampled frames individually showed possible signs" in texts(body, "model")[0]


def test_all_clean_frames_stay_none(client, frames_fake):
    frames_fake([0.01] * 8, [0.001] * 8)
    body = post(client, "v.mp4", CLIP)
    assert body["model"]["decision"] == "none" and body["model"]["frames_likely"] == 0
    assert "no strong AI signal" in texts(body, "model")[0]


def test_frontend_reading_mentions_likely_frames(client):
    js = client.get("/app.js").text
    assert "m.frames_likely" in js and "No consistent AI signal" in js


@pytest.mark.ml
@pytest.mark.parametrize(("ai_slots", "expected"), [((), "none"), ((0,), "possible"), ((0, 2, 4, 6), "likely")])
def test_real_models_on_mixed_clips(client, ai_slots, expected):
    real = ["real_bridge.jpg", "real_cat_camera_exif.jpg", "real_cat_q70_640.jpg", "real_face_truman.jpg",
            "pixel10_verified.jpg"]
    ai = ["ai_dalle_mars.jpg", "ai_midjourney_city.jpg", "ai_face_stylegan.jpg", "ai_dalle_cat_q70_640.jpg"]
    seq = [real[i % len(real)] for i in range(8)]
    for j, slot in enumerate(ai_slots):
        seq[slot] = ai[j]
    body = post(client, "mix.mp4", media.clip_from_images([FIXTURES / n for n in seq]))
    m = body["model"]
    assert m["frames_checked"] == 8 and m["decision"] == expected, m
    if expected == "none":
        assert body["verdict"] == "unknown" and m["frames_likely"] == 0
    if expected == "possible":
        assert "1 of 8 sampled frames were individually rated likely AI-generated" in body["verdict_detail"]
        assert not any("no strong AI signal" in t for t in texts(body))
    if expected == "likely":
        assert body["verdict"] == "ai"


# --- 4. non-finite detector scores are a detector failure, not a 500 -----------------------------------------


@pytest.mark.parametrize(("h", "c"), [(math.nan, 0.9), (0.99, math.nan), (math.inf, 0.9), (0.5, -math.inf)])
def test_non_finite_scores_are_a_detector_failure(client, monkeypatch, h, c):
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "predict", media.fake_predict(h, c))
    body = post(client, "x.png", media.png())
    assert body["model"] is None
    assert body["verdict"] == "unknown" and body["verdict_title"] == "Could not fully analyze"
    assert any("could not analyze" in t and "invalid score" in t for t in texts(body, "info"))
    assert not any("no strong AI signal" in t for t in texts(body))


def test_detector_rejects_non_finite_logits(monkeypatch):
    torch = pytest.importorskip("torch")
    det = model._Detector("x/y", "", "BTF_TEST_UNUSED_ID", "BTF_TEST_UNUSED_REV")
    det._processor = lambda batch, return_tensors: {}
    det._model = lambda: SimpleNamespace(logits=torch.tensor([[math.nan]]))
    with pytest.raises(ValueError, match="non-finite"):
        det.predict([Image.new("RGB", (8, 8))])


# --- 5. low-colour images: neutral wording for every detector tier -------------------------------------------


@pytest.mark.parametrize(("h", "c"), [(0.999, 0.999), (0.999, 0.01), (0.01, 0.01)])
def test_low_colour_detector_wording_is_neutral(client, monkeypatch, h, c):
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "predict", media.fake_predict(h, c))
    buf = io.BytesIO()
    Image.new("L", (96, 64), 128).save(buf, "PNG")
    body = post(client, "grey.png", buf.getvalue())
    assert body["verdict_title"] == "Detector unreliable for this image"
    assert body["model"]["low_colour"] is True
    [line] = texts(body, "model")
    assert "do not point either way" in line
    for claim in ("agree this looks AI-generated", "possible signs", "no strong AI signal", "likely"):
        assert claim not in line


def test_frontend_low_colour_reading_is_neutral(client):
    js = client.get("/app.js").text
    assert re.search(r"if \(m\.low_colour\) reading = LOW_COLOUR_READING;", js)
    neutral = re.search(r"const LOW_COLOUR_READING = '([^']*)'", js).group(1)
    assert "reliable" in neutral.lower() and "AI" not in neutral


# --- 6. BTF_C2PA_TRUST_ANCHORS: a missing or empty file trusts nothing and is never labelled "custom" ---------


@needs_c2pa
@pytest.mark.parametrize("kind", ["missing", "empty", "not_pem"])
def test_unusable_trust_override_trusts_nothing(fresh_c2pa, monkeypatch, tmp_path, caplog, kind):
    path = tmp_path / "anchors.pem"
    if kind == "empty":
        path.write_text("")
    elif kind == "not_pem":
        path.write_text("hello")
    monkeypatch.setenv("BTF_C2PA_TRUST_ANCHORS", str(path))
    with caplog.at_level(logging.ERROR, logger="banthefake.c2pa"):
        r = c2pa_check.read(FIXTURES / "pixel10_verified.jpg", "image/jpeg")
    assert r is not None and r.valid and not r.trusted and r.trust_list_version == "" and not r.capture
    assert any("BTF_C2PA_TRUST_ANCHORS" in rec.getMessage() for rec in caplog.records)


@needs_c2pa
def test_trust_override_replaces_official_list(probe_trusted):
    probe = c2pa_check.read(PROBE / "s_capture.jpg", "image/jpeg")
    assert probe.trusted and probe.trust_list_version == "custom"
    pixel = c2pa_check.read(FIXTURES / "pixel10_verified.jpg", "image/jpeg")
    assert not pixel.trusted and pixel.trust_list_version == ""


@needs_c2pa
def test_trust_override_append_mode(probe_trusted, monkeypatch, tmp_path):
    copy = tmp_path / "ca.pem"
    shutil.copy(PROBE_CA, copy)
    monkeypatch.setenv("BTF_C2PA_TRUST_ANCHORS", str(copy))
    monkeypatch.setenv("BTF_C2PA_TRUST_ANCHORS_MODE", "append")
    assert c2pa_check.read(PROBE / "s_capture.jpg", "image/jpeg").trusted
    pixel = c2pa_check.read(FIXTURES / "pixel10_verified.jpg", "image/jpeg")
    assert pixel.trusted and pixel.trust_list_version == f"{c2pa_check.TRUST_LIST_VERSION}+custom"


# --- 7. unverified claims are not presented as facts; degraded health ----------------------------------------


@needs_c2pa
def test_untrusted_capture_is_only_a_claim(client, fresh_c2pa):
    body = post(client, "s.jpg", (PROBE / "s_capture.jpg").read_bytes())
    c = body["c2pa"]
    assert c["valid"] and not c["trusted"]
    assert c["capture"] is False and c["claimed_capture"] is True
    assert body["verdict"] != "verified_capture"


@needs_c2pa
def test_invalid_capture_is_only_a_claim(client, probe_trusted):
    data = bytearray((PROBE / "s_capture.jpg").read_bytes())
    data[-2000] ^= 1
    body = post(client, "t.jpg", bytes(data))
    c = body["c2pa"]
    assert not c["valid"] and not c["trusted"] and c["capture"] is False


def test_trusted_capture_flag_requires_trust():
    store = {"active_manifest": "m", "validation_status": [], "manifests": {"m": {"assertions": [
        {"label": "c2pa.actions", "data": {"actions": [{"action": "c2pa.created", "digitalSourceType":
         "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"}]}}]}}}
    assert c2pa_check.summarize(store, "Trusted").capture is True
    for state in ("Valid", "Invalid"):
        r = c2pa_check.summarize(store, state)
        assert r.capture is False and r.claimed_capture is True


def test_frontend_labels_unverified_claims(client):
    js = client.get("/app.js").text
    assert "Claimed (not verified)" in js
    assert re.search(r"const verified = Boolean\(c\.valid && c\.trusted\)", js)
    assert re.search(r"\['Signed by', claim\(", js) and re.search(r"\['Made with', claim\(", js)
    assert re.search(r"if \(c\.capture && verified\) captureRow = 'Yes", js)


def test_health_reports_degraded_when_one_model_failed(client, monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "deps_installed", lambda: True)
    monkeypatch.setattr(model, "PRIMARY", SimpleNamespace(loaded=False, failed=True))
    monkeypatch.setattr(model, "SECONDARY", SimpleNamespace(loaded=True, failed=False))
    assert client.get("/api/health").json()["model"] == "degraded"
    monkeypatch.setattr(model, "PRIMARY", SimpleNamespace(loaded=True, failed=False))
    assert client.get("/api/health").json()["model"] == "loaded"
