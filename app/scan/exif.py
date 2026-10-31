"""Image metadata: EXIF (camera make/model, software, dates), XMP and PNG/GIF text chunks.

Also decodes images for the detector model, tolerating damaged files: truncated JPEGs, PNG text chunks
that Pillow refuses (e.g. a zTXt that inflates past its limit) and images above the decode limit
(JPEGs are decoded at reduced scale instead).
"""

import io
import os
import re
import struct
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import ExifTags, Image, ImageFile, ImageOps, ImageStat

# Refuse to scan images above this many pixels (decompression-bomb guard). Pillow itself raises
# DecompressionBombError above 2 * Image.MAX_IMAGE_PIXELS (~179 MP by default).
MAX_PIXELS = int(os.environ.get("BTF_MAX_IMAGE_PIXELS") or 120_000_000)
# Never decode more than this many pixels at full size for the model. JPEGs above it are decoded at
# 1/2, 1/4 or 1/8 scale (libjpeg draft mode); other formats above it are not analyzed by the model.
MAX_DECODE_PIXELS = int(os.environ.get("BTF_MAX_DECODE_PIXELS") or 25_000_000)
MAX_TEXT = 64 * 1024  # cap per metadata text value
MAX_PNG_TEXT_CHUNKS = 2000  # text chunks read per PNG
MAX_PNG_TEXT_TOTAL = 4 * 1024 * 1024  # total decoded PNG text kept
# Chunk-table limits: a 50 MB upload of empty 12-byte chunks is ~4 million chunks. Real PNGs have a few
# dozen (large ones a few thousand IDAT chunks). Above either limit the file is treated as damaged.
MAX_PNG_CHUNKS = 10_000
MAX_PNG_METADATA_BYTES = 16 * 1024 * 1024  # total bytes of ancillary (non-IHDR/PLTE/IDAT/IEND) chunks
PNG_CRITICAL_TYPES = (b"IHDR", b"PLTE", b"IDAT", b"IEND")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_TEXT_TYPES = (b"tEXt", b"zTXt", b"iTXt")

T = ExifTags.Base
CAMERA_DETAIL_TAGS = (T.ExposureTime, T.FNumber, T.ISOSpeedRatings, T.FocalLength, T.LensModel)
# XMP properties that name the creating software. Free-text properties (dc:description, dc:title,
# photoshop:Headline, dc:rights...) are deliberately not read: a caption mentioning "Midjourney" says
# nothing about how the file was made.
# Values are bounded ({0,1024}) so a long packet with an unterminated value cannot make findall rescan
# the rest of the text for every property name (quadratic time). Real tool names are far shorter.
XMP_TOOL_RE = re.compile(
    r"(xmp:CreatorTool|stEvt:softwareAgent|tiff:Software|exif:Software|pdf:Producer)"
    r"(?:\s*=\s*\"([^\"]{0,1024})\"|\s*>([^<]{0,1024})<)",
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
# Bounded like XMP_TOOL_RE; the leading \s* is inside the first two alternatives only, so it cannot
# trade characters with the attribute scan of the third ([^>]{0,1024}).
XMP_DST_RE = re.compile(
    r"(?:Iptc4xmpExt|photoshop):DigitalSourceType"
    r"(?:\s*=\s*\"([^\"]{0,1024})\"|\s*>([^<]{0,1024})<|[^>]{0,1024}rdf:resource=\"([^\"]{0,1024})\")"
)
SKIP_INFO_KEYS = {"exif", "icc_profile", "xmp", "XML:com.adobe.xmp", "photoshop", "adobe", "jfif_unit"}


class ImageTooLarge(Exception):
    pass


class PngChunkLimit(ValueError):
    """The PNG chunk table exceeds MAX_PNG_CHUNKS or MAX_PNG_METADATA_BYTES; the file is treated as damaged."""


class DecodeFailed(Exception):
    """The image could not be decoded for the model; `reason` is short and user-facing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ModelInput:
    image: Image.Image
    notes: list[str] = field(default_factory=list)  # user-facing caveats, e.g. "the file is truncated"
    low_colour: bool = False


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
    user_comment: str = ""
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
        values += [(self.text_field(k), v) for k, v in self.text_chunks.items() if k.lower() == "software"]
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
    png = _is_png(path)
    if png and (problem := png_chunk_problem(path)):
        # Damaged/hostile chunk table: do not hand it to Pillow or our text reader. The scan reports the
        # file as undecodable (an info note) and still checks its raw bytes.
        meta.format = "PNG"
        meta.error = f"PngChunkLimit: {problem}"
        return meta
    try:
        im = _open(path, strip_png_text=False)
    except ImageTooLarge:
        raise
    except Exception as exc:
        try:  # e.g. a PNG text chunk Pillow refuses: read the image without its text chunks
            im = _open(path, strip_png_text=True) if png else None
        except ImageTooLarge:
            raise
        except Exception:
            im = None
        if im is None:  # corrupt or unsupported: report, let other checks still run
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
        if png:
            # Our own bounded reader: also sees text after the image data, and oversized zTXt/iTXt.
            texts = _png_text_chunks(path)
            if not meta.xmp and (xmp := texts.pop("XML:com.adobe.xmp", "")):
                meta.xmp = _clean(xmp)
            texts.pop("XML:com.adobe.xmp", None)
            meta.text_chunks.update({k[:64]: _clean(v) for k, v in texts.items() if _clean(v)})
        else:
            for key, value in im.info.items():
                if key in SKIP_INFO_KEYS or not isinstance(key, str):
                    continue
                if isinstance(value, (str, bytes)) and (text := _clean(value)):
                    if isinstance(value, bytes) and key not in ("comment", "Comment"):
                        continue
                    meta.text_chunks[key[:64]] = text
        if meta.xmp:
            meta.xmp_tools = [
                (XMP_FIELDS[tag], _clean(a or b)) for tag, a, b in XMP_TOOL_RE.findall(meta.xmp) if (a or b).strip()
            ][:20]
            meta.digital_source_types = [
                _clean(next(g for g in groups if g)) for groups in XMP_DST_RE.findall(meta.xmp) if any(groups)
            ][:10]
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
    meta.user_comment = _clean(sub.get(T.UserComment))
    meta.datetime_original = _clean(sub.get(T.DateTimeOriginal))
    meta.camera_details = any(tag in sub for tag in CAMERA_DETAIL_TAGS)


def load_for_model(path: str | Path, max_side: int = 2048) -> Image.Image:
    """Decode the image (first frame) as RGB, long side at most `max_side`. Raises DecodeFailed."""
    return decode_for_model(path, max_side).image


def decode_for_model(path: str | Path, max_side: int = 2048) -> ModelInput:
    """Decode the image for the detector, retrying damaged files in tolerant mode.

    Raises ImageTooLarge (refuse the file) or DecodeFailed (the model cannot analyze it; give the reason).
    """
    try:
        image, low_colour = _decode(path, max_side, tolerant=False)
        return ModelInput(image, low_colour=low_colour)
    except (ImageTooLarge, DecodeFailed):
        raise
    except Exception as exc:
        first = exc
    try:
        img, low_colour = _decode(path, max_side, tolerant=True)
    except (ImageTooLarge, DecodeFailed):
        raise
    except Exception as exc:
        raise DecodeFailed(f"the image data could not be decoded ({type(first).__name__})") from exc
    return ModelInput(img, ["the file is damaged or truncated; the detector checked the part that could be read"], low_colour)


def _decode(path: str | Path, max_side: int, tolerant: bool) -> tuple[Image.Image, bool]:
    jpeg = _is_jpeg(path)
    # LOAD_TRUNCATED_IMAGES is process-global. Serialize every pixel load so tolerant and strict loads
    # cannot observe one another's setting.
    with _decode_lock:
        old = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = tolerant and not jpeg
        try:
            png = _is_png(path)
            if png and (problem := png_chunk_problem(path)):
                raise DecodeFailed(f"the PNG file is damaged ({problem})")
            im = _open(path, strip_png_text=tolerant and png, pad_jpeg=tolerant and jpeg)
            with im:
                check_size(im)
                w, h = im.size
                source_low_colour = im.mode in {"1", "L", "LA", "P"}
                if im.format == "JPEG":
                    scale = _jpeg_scale(w, h, max_side)
                    if scale > 1:
                        im.draft("RGB", (w // scale, h // scale))
                elif im.size[0] * im.size[1] > MAX_DECODE_PIXELS:
                    # Pillow's reduce() first decodes formats such as PNG at full resolution. Refuse them
                    # before pixel load; metadata and C2PA parsing have already completed.
                    raise DecodeFailed(
                        f"the image is {w}×{h} pixels, above the "
                        f"{MAX_DECODE_PIXELS // 1_000_000} MP the detector can decode"
                    )
                if im.size[0] * im.size[1] > MAX_DECODE_PIXELS:
                    raise DecodeFailed(
                        f"the image is {w}\u00d7{h} pixels, above the {MAX_DECODE_PIXELS // 1_000_000} MP the detector can decode"
                    )
                im.seek(0)
                out = (ImageOps.exif_transpose(im) or im).convert("RGB")
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = old
    if tolerant and _decoded_fraction(out) < 0.2:
        raise DecodeFailed("less than 20% of the image pixels could be decoded")
    out.thumbnail((max_side, max_side), Image.Resampling.BICUBIC)
    return out, source_low_colour or _low_colour(out)


def _jpeg_scale(w: int, h: int, max_side: int) -> int:
    """libjpeg scale (1, 2, 4 or 8): as small as possible, keeping the long side >= max_side, and
    always small enough to stay under MAX_DECODE_PIXELS."""
    scale = 1
    while scale < 8 and max(w, h) // (scale * 2) >= max_side:
        scale *= 2
    while scale < 8 and -(-w // scale) * -(-h // scale) > MAX_DECODE_PIXELS:
        scale *= 2
    return scale


def _open(path: str | Path, strip_png_text: bool = False, pad_jpeg: bool = False) -> Image.Image:
    try:
        if strip_png_text:
            return Image.open(io.BytesIO(_png_without_text(path)))
        if pad_jpeg:
            return Image.open(_EOIPadded(open(path, "rb")))
        return Image.open(path)
    except Image.DecompressionBombError as exc:
        raise ImageTooLarge(str(exc)) from exc


class _EOIPadded:
    """Read-only file wrapper that returns a JPEG end-of-image marker once at EOF, so libjpeg can finish
    a truncated file (the rest of the image is filled in). Scoped to one file, unlike Pillow's global
    ImageFile.LOAD_TRUNCATED_IMAGES."""

    def __init__(self, f) -> None:
        self._f = f
        self._padded = False

    def read(self, n: int = -1) -> bytes:
        data = self._f.read(n)
        if not data and n != 0 and not self._padded:
            self._padded = True
            return b"\xff\xd9"
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        self._padded = False
        return self._f.seek(offset, whence)

    def tell(self) -> int:
        return self._f.tell()

    def close(self) -> None:
        self._f.close()


_decode_lock = threading.Lock()


def _decoded_fraction(image: Image.Image) -> float:
    """Estimate rows containing decoded pixels; truncated decoders pad the missing tail uniformly."""
    if image.height <= 1:
        return 1.0
    good = 0
    for y in range(image.height):
        extrema = image.crop((0, y, image.width, y + 1)).getextrema()
        if any(hi - lo > 2 for lo, hi in extrema):
            good += 1
    return good / image.height


def _low_colour(image: Image.Image) -> bool:
    sample = image.copy()
    sample.thumbnail((256, 256))
    colours = sample.getcolors(maxcolors=257)
    if colours is not None and len(colours) <= 256:
        return True
    saturation = ImageStat.Stat(sample.convert("HSV").getchannel("S")).mean[0]
    return saturation < 12


def _head(path: str | Path, n: int = 8) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def _is_png(path: str | Path) -> bool:
    return _head(path) == PNG_SIGNATURE


def _is_jpeg(path: str | Path) -> bool:
    return _head(path, 3) == b"\xff\xd8\xff"


def _png_chunks(f):
    """Yield (type, data_offset, length) for each PNG chunk, streaming (constant memory).

    Stops at IEND, at a truncated header, or after yielding a chunk whose declared length runs past the
    end of the file (truncated files are still decoded tolerantly). Raises PngChunkLimit when the file
    has more than MAX_PNG_CHUNKS chunks or more than MAX_PNG_METADATA_BYTES of ancillary chunk data.
    """
    size = os.fstat(f.fileno()).st_size
    f.seek(len(PNG_SIGNATURE))
    count = 0
    metadata = 0
    while True:
        header = f.read(8)
        if len(header) < 8:
            return
        length, kind = struct.unpack(">I4s", header)
        count += 1
        if count > MAX_PNG_CHUNKS:
            raise PngChunkLimit(f"more than {MAX_PNG_CHUNKS} PNG chunks")
        offset = f.tell()
        if kind not in PNG_CRITICAL_TYPES:
            metadata += min(length, max(0, size - offset))
            if metadata > MAX_PNG_METADATA_BYTES:
                raise PngChunkLimit(f"more than {MAX_PNG_METADATA_BYTES} bytes of PNG metadata chunks")
        yield kind, offset, length
        end = offset + length + 4  # data + CRC
        if kind == b"IEND" or length > 0x7FFFFFFF or end > size:
            return
        f.seek(end)


def png_chunk_problem(path: str | Path) -> str | None:
    """Why the PNG's chunk table is unsafe to process (too many chunks / too much metadata), or None."""
    try:
        with open(path, "rb") as f:
            for _ in _png_chunks(f):
                pass
    except PngChunkLimit as exc:
        return str(exc)
    return None


def _png_without_text(path: str | Path) -> bytes:
    """A copy of the PNG without its tEXt/zTXt/iTXt chunks (text is read separately by _png_text_chunks).

    Streams the chunk table (never materializes it); raises PngChunkLimit for oversized tables."""
    out = io.BytesIO()
    out.write(PNG_SIGNATURE)
    with open(path, "rb") as src, open(path, "rb") as f:
        for kind, offset, length in _png_chunks(f):
            if kind in PNG_TEXT_TYPES:
                continue
            src.seek(offset - 8)
            out.write(src.read(length + 12))
    return out.getvalue()


def _inflate(data: bytes, limit: int = MAX_TEXT) -> bytes:
    """Decompress at most `limit` bytes; never inflates a bomb in full."""
    try:
        return zlib.decompressobj().decompress(data, limit)
    except zlib.error:
        return b""


def _png_text_chunks(path: str | Path) -> dict[str, str]:
    """All PNG text chunks (also those after the image data), each value capped at MAX_TEXT characters.

    Stops quietly at the chunk-table limits (extract() reports such files as damaged)."""
    texts: dict[str, str] = {}
    total = 0
    count = 0
    with open(path, "rb") as src, open(path, "rb") as f:
        try:
            for kind, offset, length in _png_chunks(f):
                if kind not in PNG_TEXT_TYPES:
                    continue
                count += 1
                if count > MAX_PNG_TEXT_CHUNKS or total > MAX_PNG_TEXT_TOTAL:
                    break
                src.seek(offset)
                parsed = _png_text_value(kind, src.read(min(length, 4 * MAX_TEXT + 1024)))
                if parsed and parsed[0] not in texts:
                    texts[parsed[0]] = parsed[1]
                    total += len(parsed[1])
        except PngChunkLimit:
            pass
    return texts


def _png_text_value(kind: bytes, data: bytes) -> tuple[str, str] | None:
    """(key, value) of one tEXt/zTXt/iTXt chunk, or None if it is malformed or empty."""
    key, _, rest = data.partition(b"\0")
    if not key or len(key) > 79:
        return None
    if kind == b"tEXt":
        value = rest[:MAX_TEXT].decode("latin-1")
    elif kind == b"zTXt":
        value = _inflate(rest[1:]).decode("latin-1") if rest[:1] == b"\0" else ""
    else:  # iTXt: flag, method, language\0, translated keyword\0, text
        if len(rest) < 2:
            return None
        compressed, body = rest[0], rest[2:]
        parts = body.split(b"\0", 2)
        if len(parts) < 3:
            return None
        raw = _inflate(parts[2]) if compressed else parts[2][:MAX_TEXT]
        value = raw.decode("utf-8", "replace")
    return (key.decode("latin-1"), value) if value else None
