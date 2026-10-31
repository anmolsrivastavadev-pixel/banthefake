import pytest

from app.scan import exif
from tests import media
from tests.conftest import FIXTURES


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_camera_exif(tmp_path):
    meta = exif.extract(write(tmp_path, "c.jpg", media.canon_jpeg()))
    assert meta.has_exif and meta.make == "Canon" and meta.model == "Canon EOS R5"
    assert meta.camera == "Canon EOS R5"
    assert meta.camera_details
    assert meta.datetime_original == "2024:05:01 10:00:00"
    assert (meta.width, meta.height) == (64, 48)


def test_software_exif(tmp_path):
    meta = exif.extract(write(tmp_path, "p.jpg", media.photoshop_jpeg()))
    assert meta.software.startswith("Adobe Photoshop")
    assert ("EXIF Software", "Adobe Photoshop 25.0 (Windows)") in meta.tool_fields()
    assert meta.camera == ""


def test_xmp_digital_source_type(tmp_path):
    meta = exif.extract(write(tmp_path, "x.jpg", media.jpeg(xmp=media.XMP_AI)))
    assert meta.xmp
    assert meta.digital_source_types == ["http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"]


@pytest.mark.parametrize("compressed", [False, True])
def test_png_text_chunks(tmp_path, compressed):
    meta = exif.extract(write(tmp_path, "sd.png", media.png({"parameters": media.SD_PARAMETERS}, compressed)))
    assert meta.text_chunks["parameters"].startswith("a cat astronaut")
    assert any(f == "PNG text \u201cparameters\u201d" and "Steps: 30" in v for f, v in meta.tool_fields())


def test_real_photoshop_fixture_xmp_tools():
    meta = exif.extract(FIXTURES / "real_cat_original_exif.jpg")
    assert "Photoshop" in meta.software
    assert any(f.startswith("XMP") and "Photoshop" in v for f, v in meta.xmp_tools)


def test_no_metadata(tmp_path):
    meta = exif.extract(write(tmp_path, "plain.png", media.png()))
    assert not meta.has_exif and not meta.xmp and meta.tool_fields() == []


def test_decompression_bomb_rejected(tmp_path):
    p = write(tmp_path, "bomb.png", media.huge_png())
    with pytest.raises(exif.ImageTooLarge):
        exif.extract(p)
    with pytest.raises(exif.ImageTooLarge):
        exif.load_for_model(p)


def test_corrupt_image_reports_error(tmp_path):
    meta = exif.extract(write(tmp_path, "bad.jpg", b"\xff\xd8\xff\xe0" + b"garbage" * 50))
    assert meta.error


def test_load_for_model_downscales(tmp_path):
    from PIL import Image

    p = tmp_path / "wide.jpg"
    Image.new("RGB", (4000, 1000), "blue").save(p)
    img = exif.load_for_model(p, max_side=1024)
    assert img.mode == "RGB" and max(img.size) == 1024
