"""Issue: brand names in free-text metadata (captions, titles, comments, copyright) made the verdict "ai".

Brand names only count in fields that name the creating software; every marker signal carries the
field it was found in and a snippet of at most 80 characters.
"""

import pytest
from PIL import ExifTags, Image, PngImagePlugin

from tests import media

T = ExifTags.Base
FREE_TEXT = "Shot on film, NOT Midjourney / DALL-E / Firefly / Leonardo / Ideogram / Veo / Runway / FaceSwap / Sora"


def scan(client, name, data):
    res = client.post("/api/scan", files={"file": (name, data, "application/octet-stream")})
    assert res.status_code == 200, res.text
    return res.json()


def png_text(key: str, value: str) -> bytes:
    import io

    info = PngImagePlugin.PngInfo()
    info.add_text(key, value)
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), "gray").save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


FREE_TEXT_FILES = {
    "iptc_caption.jpg": lambda: media.jpeg_iptc_caption(FREE_TEXT),
    "xmp_description.jpg": lambda: media.jpeg(xmp=media.xmp_packet(
        f"<dc:description><rdf:Alt><rdf:li xml:lang=\"x-default\">{FREE_TEXT}</rdf:li></rdf:Alt></dc:description>")),
    "jpeg_comment.jpg": lambda: media.with_jpeg_segment(media.jpeg(), 0xFE, FREE_TEXT.encode()),
    "exif_description.jpg": lambda: media.jpeg({T.ImageDescription: FREE_TEXT}),
    "exif_copyright.jpg": lambda: media.jpeg({T.Copyright: "(c) 2024 Ideogram Studios photography"}),
    "exif_artist.jpg": lambda: media.jpeg({T.Artist: "Leonardo Rossi"}),
    "png_title.png": lambda: png_text("Title", "Firefly season in the Smoky Mountains"),
    "png_description.png": lambda: png_text("Description", "Veo el puente desde mi ventana. " + FREE_TEXT),
    "png_comment.png": lambda: png_text("Comment", FREE_TEXT),
    "mp4_tags.mp4": lambda: media.mp4({"title": "Milan Fashion Week Runway highlights", "artist": "Leonardo Rossi",
                                       "comment": "Better than Sora", "description": FREE_TEXT}),
}


@pytest.mark.parametrize("name", sorted(FREE_TEXT_FILES))
def test_brand_names_in_free_text_are_not_ai(client, name):
    body = scan(client, name, FREE_TEXT_FILES[name]())
    assert body["verdict"] != "ai", body["signals"]
    assert not [s for s in body["signals"] if s["kind"] == "ai"]


SOFTWARE_FILES = {
    "exif_software.jpg": (lambda: media.jpeg({T.Software: "Midjourney v6"}), "EXIF Software", "Midjourney"),
    "exif_processing.jpg": (lambda: media.jpeg({T.ProcessingSoftware: "DALL-E 3"}), "EXIF ProcessingSoftware", "DALL-E"),
    "xmp_creatortool.jpg": (lambda: media.jpeg(xmp=media.xmp_packet('<xmp:CreatorTool>Adobe Firefly</xmp:CreatorTool>')),
                            "XMP CreatorTool", "Firefly"),
    "xmp_history.jpg": (lambda: media.jpeg(xmp=media.xmp_packet(
        '<xmpMM:History><rdf:Seq><rdf:li stEvt:action="created" stEvt:softwareAgent="Leonardo.Ai"/></rdf:Seq></xmpMM:History>')),
        "XMP History softwareAgent", "Leonardo"),
    "png_software.png": (lambda: png_text("Software", "NovelAI"), "PNG text \u201cSoftware\u201d", "NovelAI"),
    "mp4_handler.mp4": (lambda: media.mp4(stream_metadata={"handler_name": "Runway Gen-3 Alpha"}),
                        "video tag \u201chandler_name\u201d", "Runway"),
}


@pytest.mark.parametrize("name", sorted(SOFTWARE_FILES))
def test_brand_names_in_software_fields_are_ai_with_field_and_snippet(client, name):
    build, field, word = SOFTWARE_FILES[name]
    body = scan(client, name, build())
    assert body["verdict"] == "ai", body["signals"]
    sig = next(s for s in body["signals"] if s["kind"] == "ai")
    assert sig["field"] == field and word in sig["snippet"] and len(sig["snippet"]) <= 80
    assert field in sig["text"] and sig["snippet"] in sig["text"]


@pytest.mark.parametrize(
    ("name", "build", "field"),
    [
        ("sd.png", lambda: media.png({"parameters": media.SD_PARAMETERS}), "PNG text \u201cparameters\u201d"),
        ("x.jpg", lambda: media.jpeg(xmp=media.XMP_AI), "XMP DigitalSourceType"),
    ],
)
def test_structured_markers_name_their_field(client, name, build, field):
    body = scan(client, name, build())
    assert body["verdict"] == "ai"
    sig = next(s for s in body["signals"] if s["kind"] == "ai")
    assert sig["field"] == field and 0 < len(sig["snippet"]) <= 80


def test_edit_signal_names_field(client):
    body = scan(client, "p.jpg", media.photoshop_jpeg())
    sig = next(s for s in body["signals"] if s["kind"] == "edit")
    assert sig["field"] == "EXIF Software" and sig["snippet"] == "Adobe Photoshop 25.0 (Windows)"
