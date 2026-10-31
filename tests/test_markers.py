import os

import pytest

from app.scan import markers


def keys(hits):
    return {h.key for h in hits}


@pytest.mark.parametrize("blob", [
    b"..http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia..",
    b"..digitalsourcetype/compositeWithTrainedAlgorithmicMedia..",
    b"\0parameters\0cat\nSteps: 20, Sampler: Euler a, CFG scale: 7",
    b'{"3": {"inputs": {"seed": 1}, "class_type": "KSampler"}}',
])
def test_unstructured_raw_markers_are_ignored(blob):
    assert markers.scan_bytes(os.urandom(512) + blob + os.urandom(512)) == []


@pytest.mark.parametrize(
    "blob",
    [b"\0Midjourney Job ID: 1234\0", b"\0Created with DALL\xc2\xb7E 3\0", b"\0Adobe Firefly\0",
     b"\0made with FaceFusion 2.0\0", b"\0Leonardo.Ai\0", b"\0Ideogram\0", b"\0Stable Diffusion\0", b"\0SynthID\0"],
)
def test_brand_names_never_match_raw_bytes(blob):
    # Brand names in raw bytes are usually free text (captions, comments, copyright): not evidence.
    assert markers.scan_bytes(os.urandom(512) + blob + os.urandom(512)) == []


@pytest.mark.parametrize(
    "word",
    [b"Sora", b"Flux", b"Gemini", b"Google", b"OpenAI", b"Imagen", b"Canvas", b"Photoshop 3.0\0", b"Adobe", b"Kling",
     b"digitalsourcetype/algorithmicMedia", b"SDXL"],
)
def test_short_or_ambiguous_words_do_not_match_raw_bytes(word):
    # These may occur by chance in binary data, or are written by non-AI tools: never flagged from raw bytes.
    assert markers.scan_bytes(b"\x13\x88" + word + b"\x00\x91") == []


def test_text_markers_ai_and_edit():
    hits = markers.scan_text([("EXIF Software", "Adobe Photoshop 25.0 (Windows)"), ("XMP CreatorTool", "Sora"),
                              ("EXIF Software", "Google")])
    by_kind = {(h.kind, h.key) for h in hits}
    assert ("edit", "edit:photoshop") in by_kind
    assert ("ai", "openai") in by_kind
    assert all("Google" not in h.text or h.key == "openai" for h in hits)


@pytest.mark.parametrize("value", ["Google", "Pixel 8 Pro", "HDR+ 1.0.540104767zd", "17.1", "Lavf60.3.100", "Canvas", "Affinity Designer 2"])
def test_text_markers_ignore_ordinary_software(value):
    assert all(h.kind != "ai" for h in markers.scan_text([("EXIF Software", value)]))


def test_affinity_designer_is_editor_not_ai():
    hits = markers.scan_text([("EXIF Software", "Affinity Designer 2")])
    assert [h.kind for h in hits] == ["edit"]


def test_text_snippet_is_bounded():
    hits = markers.scan_text([("XMP CreatorTool", "x" * 500 + " Midjourney " + "y" * 500)])
    assert len(hits) == 1 and len(hits[0].text) < 250
    h = hits[0]
    assert h.field == "XMP CreatorTool" and len(h.snippet) <= markers.SNIPPET_MAX and "Midjourney" in h.snippet
    assert "XMP CreatorTool" in h.text and h.snippet in h.text


@pytest.mark.parametrize("value", ["Midjourney", "a" * 30 + " Midjourney v6", "Midjourney " + "z" * 400, "x\n\tMidjourney\0"])
def test_snippet_never_exceeds_80_chars(value):
    (h,) = markers.scan_text([("EXIF Software", value)])
    assert 0 < len(h.snippet) <= 80 and "Midjourney" in h.snippet


def test_read_window_head_and_tail_only(tmp_path):
    p = tmp_path / "big.bin"
    with p.open("wb") as f:
        f.write(b"\0" * 1000 + b"\0Steps: 20, Sampler: Euler\0")
        f.write(b"\0" * (markers.RAW_WINDOW * 2))
        f.write(b'\0{"inputs": {}, "class_type": "KSampler"}\0')
        f.write(b"\0" * (markers.RAW_WINDOW * 2))
        f.write(b"\0digitalsourcetype/compositeWithTrainedAlgorithmicMedia\0")
    assert markers.scan_raw(p) == []


def test_c2pa_hint():
    assert markers.has_c2pa_hint(b"..jumb....c2pa..")
    assert not markers.has_c2pa_hint(b"just some bytes")
