from pathlib import Path

HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
MP4_BRANDS = {
    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42",
    b"avc1", b"M4V ", b"M4VH", b"M4VP", b"dash", b"mmp4", b"3gp4", b"3gp5", b"3gp6", b"3g2a",
}
QT_ATOMS = {b"moov", b"mdat", b"wide", b"pnot"}


def _ftyp_brands(head: bytes) -> list[bytes]:
    size = int.from_bytes(head[0:4], "big")
    end = min(max(size, 16), len(head))
    major = head[8:12]
    compatible = [head[i : i + 4] for i in range(16, end - 3, 4)]
    return [major, *compatible]


def sniff(head: bytes) -> tuple[str, str] | None:
    """Return (kind, mime) for supported media based on magic bytes, else None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp"
    if head[:4] == b"\x1a\x45\xdf\xa3" and b"webm" in head[:64]:
        return "video", "video/webm"
    if head[4:8] == b"ftyp":
        brands = _ftyp_brands(head)
        major = brands[0]
        if major in HEIF_BRANDS:
            return "image", "image/heif" if major in (b"mif1", b"msf1") else "image/heic"
        if major == b"qt  ":
            return "video", "video/quicktime"
        if major in MP4_BRANDS or (major not in (b"M4A ", b"M4B ", b"M4P ") and MP4_BRANDS & set(brands[1:])):
            return "video", "video/mp4"
        return None
    if head[4:8] in QT_ATOMS:
        return "video", "video/quicktime"
    return None


def pillow_can_open(path: Path) -> bool:
    from PIL import Image

    try:
        with Image.open(path) as img:
            img.verify()
    except Exception:
        return False
    return True
