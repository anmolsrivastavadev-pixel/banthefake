"""Synthetic media builders for tests."""

import io
import struct
import zlib

from PIL import ExifTags, Image, PngImagePlugin

XMP_AI = (
    '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/" '
    'Iptc4xmpExt:DigitalSourceType="http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"/>'
    '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)
SD_PARAMETERS = (
    "a cat astronaut, highly detailed\nNegative prompt: blurry\n"
    "Steps: 30, Sampler: DPM++ 2M Karras, CFG scale: 7, Seed: 1234, Size: 512x512, Model: sd_xl_base_1.0"
)


def _gradient(size=(64, 48)) -> Image.Image:
    im = Image.new("RGB", size)
    im.putdata([(x * 4 % 256, y * 5 % 256, (x + y) % 256) for y in range(size[1]) for x in range(size[0])])
    return im


def jpeg(exif: dict[int, str] | None = None, xmp: str | None = None, exif_ifd: dict[int, object] | None = None) -> bytes:
    im = _gradient()
    kwargs = {}
    if exif or exif_ifd:
        e = Image.Exif()
        for k, v in (exif or {}).items():
            e[k] = v
        if exif_ifd:
            e.get_ifd(ExifTags.IFD.Exif).update(exif_ifd)
        kwargs["exif"] = e
    if xmp:
        kwargs["xmp"] = xmp.encode()
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, **kwargs)
    return buf.getvalue()


def png(text: dict[str, str] | None = None, compressed: bool = False) -> bytes:
    info = PngImagePlugin.PngInfo()
    for k, v in (text or {}).items():
        if compressed:
            info.add_itxt(k, v, zip=True)
        else:
            info.add_text(k, v)
    buf = io.BytesIO()
    _gradient().save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


def canon_jpeg() -> bytes:
    T = ExifTags.Base
    return jpeg(
        {T.Make: "Canon", T.Model: "Canon EOS R5", T.Software: "Firmware Version 1.8.1"},
        exif_ifd={T.ExposureTime: 0.004, T.FNumber: 2.8, T.DateTimeOriginal: "2024:05:01 10:00:00"},
    )


def photoshop_jpeg() -> bytes:
    return jpeg({ExifTags.Base.Software: "Adobe Photoshop 25.0 (Windows)"})


def huge_png(width: int = 20000, height: int = 20000) -> bytes:
    """A tiny PNG whose header claims a huge size (decompression-bomb shape)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\0" * 64)) + chunk(b"IEND", b"")


def xmp_packet(body: str) -> str:
    """An XMP packet whose rdf:Description contains `body` (attributes/elements, Adobe namespaces declared)."""
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/" xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#">'
        f"{body}</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"
    )


def with_jpeg_segment(data: bytes, marker: int, payload: bytes) -> bytes:
    """Insert an APPn/COM segment right after the JPEG SOI marker."""
    assert data[:2] == b"\xff\xd8"
    return data[:2] + bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload + data[2:]


def jpeg_iptc_caption(caption: str) -> bytes:
    """JPEG with an IPTC-IIM caption (record 2, dataset 120) in a Photoshop APP13 segment."""
    text = caption.encode()
    iim = b"\x1c\x02\x78" + struct.pack(">H", len(text)) + text
    if len(iim) % 2:
        iim += b"\0"
    irb = b"8BIM" + struct.pack(">H", 0x0404) + b"\0\0" + struct.pack(">I", len(iim)) + iim
    return with_jpeg_segment(jpeg(), 0xED, b"Photoshop 3.0\0" + irb)


def mp4(metadata: dict[str, str] | None = None, stream_metadata: dict[str, str] | None = None,
        seconds: float = 1.0, fps: int = 10, size: tuple[int, int] = (64, 48), gop: int | None = None) -> bytes:
    """A small MPEG-4 video built with PyAV (no ffmpeg binary needed)."""
    import tempfile
    from pathlib import Path

    import av
    import numpy as np

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "v.mp4"
        with av.open(str(path), "w", format="mp4") as out:
            for k, v in (metadata or {}).items():
                out.metadata[k] = v
            st = out.add_stream("mpeg4", rate=fps)
            st.width, st.height, st.pix_fmt = size[0], size[1], "yuv420p"
            if gop:
                st.codec_context.gop_size = gop
            for k, v in (stream_metadata or {}).items():
                st.metadata[k] = v
            w, h = size
            for i in range(int(seconds * fps)):
                arr = np.zeros((h, w, 3), np.uint8)
                arr[..., 0] = (np.arange(w) * 4 + i * 7) % 256
                arr[..., 1] = (np.arange(h)[:, None] * 5 + i * 3) % 256
                for packet in st.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                    out.mux(packet)
            for packet in st.encode():
                out.mux(packet)
        return path.read_bytes()


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def insert_png_chunk(data: bytes, chunk: bytes, after_image: bool = False) -> bytes:
    """Insert a raw chunk right after IHDR, or right before IEND."""
    if after_image:
        i = data.rindex(b"IEND") - 4
        return data[:i] + chunk + data[i:]
    return data[:33] + chunk + data[33:]


def ztxt_bomb(key: str = "Comment", inflated: int = 8 * 1024 * 1024) -> bytes:
    """A zTXt chunk that inflates far past Pillow's MAX_TEXT_CHUNK (1 MB)."""
    return png_chunk(b"zTXt", key.encode() + b"\0\0" + zlib.compress(b"A" * inflated, 9))


def photo_png(size=(256, 192)) -> bytes:
    buf = io.BytesIO()
    _gradient(size).save(buf, "PNG")
    return buf.getvalue()


def photo_jpeg(size=(256, 192)) -> bytes:
    buf = io.BytesIO()
    _gradient(size).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def audio_only_mp4(seconds: float = 1.0) -> bytes:
    """An MP4 (isom brand) with an AAC audio track and no video track."""
    import tempfile
    from pathlib import Path

    import av
    import numpy as np

    rate = 44100
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "a.mp4"
        with av.open(str(path), "w", format="mp4") as out:
            st = out.add_stream("aac", rate=rate)
            t = np.arange(int(seconds * rate)) / rate
            samples = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)[None, :]
            frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="mono")
            frame.sample_rate = rate
            for packet in st.encode(frame):
                out.mux(packet)
            for packet in st.encode():
                out.mux(packet)
        return path.read_bytes()


def fake_predict(h: float | None, c: float | None = -1.0):
    """A stand-in for `model.predict` giving every image the scores `h` (primary) and `c` (secondary).

    `c` defaults to `h`; pass None for either to simulate that model failing to load.
    """
    from app.scan import model

    if c == -1.0:
        c = h

    def predict(images):
        n = len(images)
        return model.Prediction(h=None if h is None else [h] * n, c=None if c is None else [c] * n)

    return predict
