"""End-to-end through /api/scan for each verdict path (model off; see test_model.py for the ML paths)."""

import re

import pytest

from app import scan
from app.scan import model
from tests import media
from tests.conftest import FIXTURES

REAL_CLAIM = re.compile(r"\b(is|looks|appears) (real|authentic|genuine)\b|\bverified real\b", re.I)
NEGATED = re.compile(r"(does not mean (it|the file) is real|can prove a file is real)", re.I)


def post(client, name, data, mime="application/octet-stream"):
    res = client.post("/api/scan", files={"file": (name, data, mime)})
    assert res.status_code == 200, res.text
    body = res.json()
    assert scan.NO_PROOF in body["verdict_detail"]
    for text in [body["verdict_title"], body["verdict_detail"], *(s["text"] for s in body["signals"])]:
        assert not REAL_CLAIM.search(NEGATED.sub("", text)), text
    for s in body["signals"]:
        assert s["kind"] in {"ai", "edit", "camera", "c2pa", "model", "info"} and 0 <= s["weight"] <= 1
    return body


def kinds(body):
    return [s["kind"] for s in body["signals"]]


def test_ai_from_stable_diffusion_png_text(client):
    body = post(client, "sd.png", media.png({"parameters": media.SD_PARAMETERS}))
    assert body["verdict"] == "ai" and body["verdict_title"] == "AI-generation markers found"
    assert any("Stable Diffusion" in s["text"] for s in body["signals"])
    assert body["model"] is None  # BTF_MODEL=off in regular tests


def test_ai_from_compressed_png_itxt(client):
    body = post(client, "sd.png", media.png({"parameters": media.SD_PARAMETERS}, compressed=True))
    assert body["verdict"] == "ai"


def test_ai_from_comfyui_workflow(client):
    prompt = '{"3": {"inputs": {"seed": 5, "steps": 20}, "class_type": "KSampler"}}'
    body = post(client, "comfy.png", media.png({"prompt": prompt}, compressed=True))
    assert body["verdict"] == "ai" and any("ComfyUI" in s["text"] for s in body["signals"])


def test_ai_from_xmp_digital_source_type(client):
    body = post(client, "x.jpg", media.jpeg(xmp=media.XMP_AI))
    assert body["verdict"] == "ai"
    assert any("trainedAlgorithmicMedia" in s["text"] for s in body["signals"] if s["kind"] == "ai")


def test_ai_from_generator_software_tag(client):
    body = post(client, "fl.jpg", media.jpeg({305: "FLUX.1 [dev]"}))
    assert body["verdict"] == "ai"


def test_ai_c2pa_declaration(client, monkeypatch):
    from app.scan import c2pa_check

    declared = c2pa_check.summarize(
        {
            "active_manifest": "m",
            "manifests": {"m": {"claim_generator": "ChatGPT", "signature_info": {"issuer": "OpenAI"}, "assertions": [
                {"label": "c2pa.actions", "data": {"actions": [{"action": "c2pa.created", "digitalSourceType":
                    "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"}]}}]}},
        },
        "Trusted",
    )
    monkeypatch.setattr(c2pa_check, "read", lambda path, mime: declared)
    body = post(client, "c.png", media.png())
    assert body["verdict"] == "ai"
    assert body["c2pa"]["ai_generated"] is True and body["c2pa"]["issuer"] == "OpenAI"
    assert "content credentials declare" in body["verdict_detail"].lower()
    assert kinds(body).count("ai") == 1  # C2PA claim and raw IPTC marker are not double-counted


def test_edited_photoshop_software(client):
    body = post(client, "p.jpg", media.photoshop_jpeg())
    assert body["verdict"] == "edited" and "edit" in kinds(body)


def test_edited_real_fixture_with_photoshop_xmp(client):
    body = post(client, "truman.jpg", (FIXTURES / "real_face_truman.jpg").read_bytes())
    assert body["verdict"] == "edited"
    assert body["file"]["width"] == 350


def test_edited_c2pa_color_adjustments(client):
    body = post(client, "CA.jpg", (FIXTURES / "CA.jpg").read_bytes())
    assert body["verdict"] == "edited"
    c = body["c2pa"]
    assert c["present"] and c["valid"] and not c["trusted"] and not c["ai_generated"]
    assert "c2pa.color_adjustments" in c["actions"]
    assert any("not on our trust list" in s["text"] for s in body["signals"] if s["kind"] == "c2pa")


def test_c2pa_algorithmic_media_is_not_ai(client):
    body = post(client, "C.jpg", (FIXTURES / "C.jpg").read_bytes())
    assert body["verdict"] == "unknown"
    assert body["c2pa"]["ai_generated"] is False and body["c2pa"]["generator"].startswith("make_test_images")
    assert "ai" not in kinds(body)


def test_camera_exif(client):
    body = post(client, "c.jpg", media.canon_jpeg())
    assert body["verdict"] == "camera"
    assert body["verdict_title"] == "Camera data found, no AI markers"
    assert "does NOT mean the file is real" in body["verdict_detail"]
    assert any("Canon EOS R5" in s["text"] for s in body["signals"])


def test_camera_plus_editor_is_edited(client):
    from PIL import ExifTags

    T = ExifTags.Base
    body = post(client, "c.jpg", media.jpeg({T.Make: "NIKON CORPORATION", T.Model: "NIKON D850", T.Software: "Adobe Lightroom Classic 13.0"}))
    assert body["verdict"] == "edited" and "camera" in kinds(body)


def test_unknown_plain_png_no_metadata(client):
    body = post(client, "plain.png", media.png())
    assert body["verdict"] == "unknown" and body["verdict_title"] == "No conclusive signals found"
    assert "does NOT mean it is real" in body["verdict_detail"]
    assert kinds(body) == ["info"]  # "no metadata at all"


@pytest.mark.parametrize("name", ["real_bridge.jpg", "ai_midjourney_city.jpg", "real_cat_q70_640.jpg"])
def test_fixtures_without_markers_are_unknown(client, name):
    body = post(client, name, (FIXTURES / name).read_bytes())
    assert body["verdict"] == "unknown" and body["c2pa"] is None


@pytest.mark.parametrize(("name", "mime"), [("test_clip.mp4", "video/mp4"), ("test_clip.mov", "video/quicktime")])
def test_video(client, name, mime):
    body = post(client, name, (FIXTURES / name).read_bytes())
    assert body["verdict"] == "unknown"
    f = body["file"]
    assert f["type"] == "video" and f["mime"] == mime
    assert (f["width"], f["height"]) == (640, 640) and f["duration_s"] == pytest.approx(5.0, abs=0.1)
    assert body["model"] is None


def test_decompression_bomb_415(client):
    res = client.post("/api/scan", files={"file": ("bomb.png", media.huge_png(), "image/png")})
    assert res.status_code == 415 and "too large" in res.json()["error"]


def test_corrupt_image_still_reports(client):
    body = post(client, "bad.jpg", b"\xff\xd8\xff\xe0" + b"garbage" * 50)
    assert body["verdict"] == "unknown" and "info" in kinds(body)


def test_model_unavailable_degrades(client, monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "deps_installed", lambda: False)
    monkeypatch.setattr(model, "_model", None)  # as if torch was never importable
    assert scan.capabilities()["model"] == "unavailable"
    assert client.get("/api/health").json()["model"] == "unavailable"
    body = post(client, "plain.png", media.png())
    assert body["model"] is None


def test_model_off_capability(client):
    assert scan.capabilities()["model"] == "off"
    assert client.get("/api/health").json() == {"ok": True, "model": "off", "c2pa": True}
