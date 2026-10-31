"""Image metadata: EXIF (camera make/model, software, dates), XMP and PNG/GIF text chunks."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

# Refuse to scan images above this many pixels (decompression-bomb guard). Pillow itself raises
# DecompressionBombError above 2 * Image.MAX_IMAGE_PIXELS (~179 MP by default).
MAX_PIXELS = int(os.environ.get("BTF_MAX_IMAGE_PIXELS") or 120_000_000)
# Non-JPEG images above this are not decoded for the model (JPEG uses draft-mode downscaling instead).
MAX_DECODE_PIXELS = 50_000_000
MAX_TEXT = 64 * 1024  # cap per metadata text value

T = ExifTags.Base
CAMERA_DETAIL_TAGS = (T.ExposureTime, T.FNumber, T.ISOSpeedRatings, T.FocalLength, T.LensModel)
# XMP properties that name the creating software. Free-text properties (dc:description, dc:title,
# photoshop:Headline, dc:rights...) are deliberately not read: a caption mentioning "Midjourney" says
# nothing about how the file was made.
XMP_TOOL_RE = re.compile(
    r"(xmp:CreatorTool|stEvt:softwareAgent|tiff:Software|exif:Software|pdf:Producer)"
    r"\s*(?:=\s*\"([^\"]*)\"|>([^<]*)<)",
)
XMP_FIELDS = {
    "xmp:CreatorTool": "XMP CreatorTool",
    "stEvt:softwareAgent": "XMP History softwareAgent",
    "tiff:Software": "XMP tiff:Software",
    "exif:Software": "XMP exif:Software",
    "pdf:Producer": "XMP pdf:Producer",
}
# PNG text keys written by AI generators (A1111/Forge "parameters", ComfyUI "prompt"/"workflow",
# NovelAI and others "Software"). Other keys (Title, Description, Comment, Author...) are free text.
PNG_TOOL_KEYS = {"parameters", "prompt", "workflow", "software"}
XMP_DST_RE = re.compile(r"DigitalSourceType\s*(?:=\s*\"([^\"]*)\"|>([^<]*)<|[^>]*rdf:resource=\"([^\"]*)\")")
SKIP_INFO_KEYS = {"exif", "icc_profile", "xmp", "XML:com.adobe.xmp", "photoshop", "adobe", "jfif_unit"}


class ImageTooLarge(Exception):
    pass


@dataclass
class ImageMeta:
    format: str | None = None
    width: int | None = None
    height: int | None = None
    has_exif: bool = False
    make: str = ""
    model: str = ""
    software: str = ""
    processing_software: str = ""
    datetime_original: str = ""
    camera_details: bool = False
    xmp: str = ""
    xmp_tools: list[tuple[str, str]] = field(default_factory=list)  # (field label, value)
    digital_source_types: list[str] = field(default_factory=list)
    text_chunks: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def camera(self) -> str:
        make, model = self.make.strip(), self.model.strip()
        if model.lower().startswith(make.lower()):
            return model
        return f"{make} {model}".strip()

    def tool_fields(self) -> list[tuple[str, str]]:
        """`(field, value)` pairs for metadata fields that name the software that produced the file."""
        values = [("EXIF Software", self.software), ("EXIF ProcessingSoftware", self.processing_software)]
        values += self.xmp_tools
        values += [("XMP DigitalSourceType", v) for v in self.digital_source_types]
        values += [(self.text_field(k), v) for k, v in self.text_chunks.items() if k.lower() in PNG_TOOL_KEYS]
        return [(f, v) for f, v in values if v]

    def text_field(self, key: str) -> str:
        if self.format == "PNG":
            return f"PNG text \u201c{key}\u201d"
        if key.lower() == "comment":
            return f"{self.format or 'image'} comment"
        return f"{self.format or 'image'} text \u201c{key}\u201d"


def _clean(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()[:MAX_TEXT]


def _xmp_text(im: Image.Image) -> str:
    raw = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp") or b""
    return _clean(raw) if raw else ""


def check_size(im: Image.Image) -> None:
    w, h = im.size
    if w * h > MAX_PIXELS:
        raise ImageTooLarge(f"{w}x{h}")


def extract(path: str | Path) -> ImageMeta:
    """Read metadata without decoding pixels. Raises ImageTooLarge for oversized images."""
    meta = ImageMeta()
    try:
        im = Image.open(path)
    except Image.DecompressionBombError as exc:
        raise ImageTooLarge(str(exc)) from exc
    except Exception as exc:  # corrupt or unsupported: report, let other checks still run
        meta.error = type(exc).__name__
        return meta
    with im:
        meta.format = im.format
        meta.width, meta.height = im.size
        check_size(im)
        try:
            _read_exif(im, meta)
        except Exception:  # malformed EXIF must not break the scan
            pass
        meta.xmp = _xmp_text(im)
        if meta.xmp:
            meta.xmp_tools = [
                (XMP_FIELDS[tag], _clean(a or b)) for tag, a, b in XMP_TOOL_RE.findall(meta.xmp) if (a or b).strip()
            ][:20]
            meta.digital_source_types = [
                _clean(next(g for g in groups if g)) for groups in XMP_DST_RE.findall(meta.xmp) if any(groups)
            ][:10]
        for key, value in im.info.items():
            if key in SKIP_INFO_KEYS or not isinstance(key, str):
                continue
            if isinstance(value, (str, bytes)) and (text := _clean(value)):
                if isinstance(value, bytes) and key not in ("comment", "Comment"):
                    continue
                meta.text_chunks[key[:64]] = text
    return meta


def _read_exif(im: Image.Image, meta: ImageMeta) -> None:
    exif = im.getexif()
    if not exif:
        return
    meta.has_exif = True
    meta.make = _clean(exif.get(T.Make))
    meta.model = _clean(exif.get(T.Model))
    meta.software = _clean(exif.get(T.Software))
    meta.processing_software = _clean(exif.get(T.ProcessingSoftware))
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    meta.datetime_original = _clean(sub.get(T.DateTimeOriginal))
    meta.camera_details = any(tag in sub for tag in CAMERA_DETAIL_TAGS)


def load_for_model(path: str | Path, max_side: int = 2048) -> Image.Image | None:
    """Decode the image (first frame) as RGB, downscaled so the long side is at most `max_side`."""
    try:
        im = Image.open(path)
    except Image.DecompressionBombError as exc:
        raise ImageTooLarge(str(exc)) from exc
    with im:
        check_size(im)
        if im.format == "JPEG":
            im.draft("RGB", (max_side, max_side))
        elif im.size[0] * im.size[1] > MAX_DECODE_PIXELS:
            return None
        im.seek(0)
        out = (ImageOps.exif_transpose(im) or im).convert("RGB")
    out.thumbnail((max_side, max_side), Image.Resampling.BICUBIC)
    return out
