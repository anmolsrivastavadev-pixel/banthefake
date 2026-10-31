import pytest

from app.scan import c2pa_check
from tests.conftest import FIXTURES

pytestmark = pytest.mark.skipif(not c2pa_check.AVAILABLE, reason="c2pa-python not installed")

AI = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"


def store(actions, status=None, generator="ChatGPT"):
    return {
        "active_manifest": "urn:1",
        "manifests": {
            "urn:1": {
                "claim_generator_info": [{"name": generator, "version": "1.0"}],
                "signature_info": {"issuer": "OpenAI"},
                "assertions": [{"label": "c2pa.actions.v2", "data": {"actions": actions}}],
            }
        },
        "validation_status": status or [],
    }


def test_c_jpg_algorithmic_but_not_generative_ai():
    r = c2pa_check.read(FIXTURES / "C.jpg", "image/jpeg")
    assert r is not None and r.present and r.readable
    assert r.valid and not r.trusted and r.untrusted  # test signing cert is not on a trust list
    assert r.issuer == "C2PA Test Signing Cert"
    assert r.generator.startswith("make_test_images")
    assert r.actions == ["c2pa.created"]
    assert r.source_types[0].endswith("/algorithmicMedia")
    assert r.ai_generated is False


def test_ca_jpg_records_edits():
    r = c2pa_check.read(FIXTURES / "CA.jpg", "image/jpeg")
    assert r.valid and not r.ai_generated
    assert r.edit_actions == ["c2pa.color_adjustments"]


@pytest.mark.parametrize(("name", "mime"), [("real_bridge.jpg", "image/jpeg"), ("test_clip.mp4", "video/mp4")])
def test_no_manifest_is_none(name, mime):
    assert c2pa_check.read(FIXTURES / name, mime) is None


def test_unparseable_is_flagged_unreadable(tmp_path):
    p = tmp_path / "x.gif"
    p.write_bytes(b"GIF89a" + b"\0" * 100)
    r = c2pa_check.read(p, "image/gif")
    assert r is None or not r.readable


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (AI, True),
        ("http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia", True),
        ("http://cv.iptc.org/newscodes/digitalsourcetype/algorithmicMedia", False),
        ("http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture", False),
    ],
)
def test_ai_generated_source_types(source, expected):
    r = c2pa_check.summarize(store([{"action": "c2pa.created", "digitalSourceType": source}]), "Valid")
    assert r.ai_generated is expected


def test_failed_validation_is_invalid():
    r = c2pa_check.summarize(
        store([{"action": "c2pa.created", "digitalSourceType": AI}], [{"code": "assertion.dataHash.mismatch"}]),
        "Invalid",
    )
    assert not r.valid and r.failures == ["assertion.dataHash.mismatch"] and r.ai_generated


def test_report_shape_and_agents():
    r = c2pa_check.summarize(
        store([{"action": "c2pa.created", "softwareAgent": {"name": "GPT-4o"}}, {"action": "c2pa.resized"}]), "Trusted"
    )
    assert r.trusted and r.software_agents == ["GPT-4o"] and r.edit_actions == ["c2pa.resized"]
    assert set(r.to_report()) == {
        "present", "valid", "trusted", "generator", "issuer", "signer", "signed_at", "capture",
        "ai_generated", "actions", "edits", "ingredients", "trust_list_version",
    }
    assert r.generator == "ChatGPT 1.0"


def test_official_pixel_capture_is_trusted():
    r = c2pa_check.read(FIXTURES / "pixel10_verified.jpg", "image/jpeg")
    assert r is not None and r.valid and r.trusted and r.capture and not r.ai_generated
    assert r.signer == "Pixel Camera (Google LLC)"
    assert r.signed_at == "2026-10-18T06:51:42+00:00"
    assert r.trust_list_version == "2026-10-23"


def test_test_certificates_stay_untrusted():
    for name in ("C.jpg", "CA.jpg"):
        r = c2pa_check.read(FIXTURES / name, "image/jpeg")
        assert r is not None and r.valid and not r.trusted and r.untrusted


def test_tampered_official_pixel_is_invalid(tmp_path):
    data = bytearray((FIXTURES / "pixel10_verified.jpg").read_bytes())
    data[-1000] ^= 1
    path = tmp_path / "tampered.jpg"
    path.write_bytes(data)
    r = c2pa_check.read(path, "image/jpeg")
    assert r is not None and not r.valid and not r.trusted
    assert "assertion.dataHash.mismatch" in r.failures


def test_trust_can_be_disabled(monkeypatch):
    monkeypatch.setenv("BTF_C2PA_TRUST", "off")
    r = c2pa_check.read(FIXTURES / "pixel10_verified.jpg", "image/jpeg")
    assert r is not None and r.valid and not r.trusted and not r.trust_list_version
