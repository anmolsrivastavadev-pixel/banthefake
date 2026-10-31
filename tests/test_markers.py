import os

import pytest

from app.scan import markers


def keys(hits):
    return {h.key for h in hits}


@pytest.mark.parametrize(
    ("blob", "key"),
    [
        (b"..http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia..", "iptc_trained"),
        (b"..digitalsourcetype/compositeWithTrainedAlgorithmicMedia..", "iptc_composite"),
        (b"\0parameters\0cat\nSteps: 20, Sampler: Euler a, CFG scale: 7", "sd_params"),
        (b'{"3": {"inputs": {"seed": 1}, "class_type": "KSampler"}}', "comfyui"),
        (b"\0Midjourney Job ID: 1234\0", "midjourney"),
        (b"\0Created with DALL\xc2\xb7E 3\0", "dalle"),
        (b"\0Adobe Firefly\0", "firefly"),
        (b"\0made with FaceFusion 2.0\0", "faceswap"),
    ],
)
def test_raw_markers_hit(blob, key):
    assert key in keys(markers.scan_bytes(os.urandom(512) + blob + os.urandom(512)))


@pytest.mark.parametrize(
    "word",
    [b"Sora", b"Flux", b"Gemini", b"Google", b"OpenAI", b"Imagen", b"Canvas", b"Photoshop 3.0\0", b"Adobe", b"Kling",
     b"digitalsourcetype/algorithmicMedia", b"SDXL"],
)
def test_short_or_ambiguous_words_do_not_match_raw_bytes(word):
    # These may occur by chance in binary data, or are written by non-AI tools: never flagged from raw bytes.
    assert markers.scan_bytes(b"\x13\x88" + word + b"\x00\x91") == []


def test_text_markers_ai_and_edit():
    hits = markers.scan_text(["Adobe Photoshop 25.0 (Windows)", "Sora", "Google"])
    by_kind = {(h.kind, h.key) for h in hits}
    assert ("edit", "edit:photoshop") in by_kind
    assert ("ai", "openai") in by_kind
    assert all("Google" not in h.text or h.key == "openai" for h in hits)


@pytest.mark.parametrize("value", ["Google", "Pixel 8 Pro", "HDR+ 1.0.540104767zd", "17.1", "Lavf60.3.100", "Canvas", "Affinity Designer 2"])
def test_text_markers_ignore_ordinary_software(value):
    assert all(h.kind != "ai" for h in markers.scan_text([value]))


def test_affinity_designer_is_editor_not_ai():
    hits = markers.scan_text(["Affinity Designer 2"])
    assert [h.kind for h in hits] == ["edit"]


def test_text_snippet_is_bounded():
    hits = markers.scan_text(["x" * 500 + " Midjourney " + "y" * 500])
    assert len(hits) == 1 and len(hits[0].text) < 200


def test_read_window_head_and_tail_only(tmp_path):
    p = tmp_path / "big.bin"
    middle = b"\0Midjourney\0"
    with p.open("wb") as f:
        f.write(b"\0" * 1000 + b"\0Adobe Firefly\0")
        f.write(b"\0" * (markers.RAW_WINDOW * 2))
        f.write(middle)
        f.write(b"\0" * (markers.RAW_WINDOW * 2))
        f.write(b"\0FaceFusion\0")
    found = keys(markers.scan_raw(p))
    assert "firefly" in found and "faceswap" in found
    assert "midjourney" not in found  # the middle of large files is not scanned


def test_c2pa_hint():
    assert markers.has_c2pa_hint(b"..jumb....c2pa..")
    assert not markers.has_c2pa_hint(b"just some bytes")
