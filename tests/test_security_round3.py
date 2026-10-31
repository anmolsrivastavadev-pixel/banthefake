"""Security review, round 3: regex DoS, PNG chunk-table amplification, rate-limit retention, admin token."""

import io
import logging
import random
import re
import struct
import time
import tracemalloc
import zlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import petition, ratelimit
from app.config import Settings
from app.main import create_app
from app.scan import exif, markers
from tests import media

N = 64 * 1024
FAST = 0.2  # seconds; the vulnerable patterns took from seconds to hours on these inputs


def _timed(fn, *args) -> float:
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


# --- 1. Regular-expression denial of service --------------------------------------------------------

A1111_ADVERSARIAL = {
    "newlines": b"x" + b"\n" * N,
    "steps_headers_without_cfg": b"x" + b"\nSteps: 1, Sampler: a," * (N // 22),
    "sampler_whitespace": b"x\nSteps: 1, Sampler:" + b" " * N,
    "sampler_whitespace_newlines": b"x\nSteps: 1, Sampler:" + b" \n" * (N // 2),
    "cfg_without_comma": b"x\nSteps: 1, Sampler: a, " + b"CFG scale: 1 " * (N // 13),
    "cfg_digits": b"x\nSteps: 1, Sampler: a, CFG scale: " + b"1" * N,
    "seed_without_digit": b"x\nSteps: 1, Sampler: a, CFG scale: 1, " + b"Seed: " * (N // 6),
    "steps_digits": b"x\nSteps: " + b"1" * N,
}
COMFY_ADVERSARIAL = {
    "class_type_unterminated_name": b'"class_type":"' + b"A" * N,
    "class_type_letters_then_space": b'"class_type":"' + b"A" * (N // 2) + b" " * (N // 2),
    "inputs_repeated": b'"inputs"' * (N // 8),
    "class_types_repeated": b'"class_type":"A"' * (N // 16),
    "class_keys_repeated": b'"class_type" :' * (N // 14),
    "class_type_whitespace": b'"class_type"' + b" " * N,
}


@pytest.mark.parametrize("name", sorted(A1111_ADVERSARIAL))
@pytest.mark.parametrize("key", ["parameters", "usercomment"])
def test_a1111_matcher_is_linear_on_adversarial_input(name, key):
    data = A1111_ADVERSARIAL[name]
    assert len(data) >= N - 32
    assert _timed(markers.scan_generator_metadata, data, "PNG text", key) < FAST


@pytest.mark.parametrize("name", sorted(COMFY_ADVERSARIAL))
def test_comfy_matcher_is_linear_on_adversarial_input(name):
    data = COMFY_ADVERSARIAL[name]
    assert _timed(markers.scan_generator_metadata, data, "PNG text", "workflow") < FAST


@pytest.mark.parametrize("data", [b"jumb" * (N // 4), b"c2pa." * (N // 5), (b"jumb" + b"x" * 60) * (N // 64)])
def test_c2pa_hint_is_fast(data):
    assert _timed(markers.has_c2pa_hint, data) < FAST


XMP_ADVERSARIAL = [
    'xmp:CreatorTool="' * (N // 17),
    "xmp:CreatorTool>" * (N // 16),
    'xmp:CreatorTool = "' * (N // 19),
    "xmp:CreatorTool" + " " * N,
    "photoshop:DigitalSourceType " * (N // 28),
    'photoshop:DigitalSourceType="' * (N // 29),
    "photoshop:DigitalSourceType>" * (N // 28),
    'Iptc4xmpExt:DigitalSourceType rdf:resource="' * (N // 45),
    "photoshop:DigitalSourceType" + " " * N,
]


@pytest.mark.parametrize("i", range(len(XMP_ADVERSARIAL)))
def test_xmp_regexes_are_fast(i):
    text = XMP_ADVERSARIAL[i]
    assert _timed(exif.XMP_TOOL_RE.findall, text) < FAST
    assert _timed(exif.XMP_DST_RE.findall, text) < FAST


TEXT_ADVERSARIAL = [
    "a" * N,
    " " * N,
    "Veo " + "1" * N,
    "Veo 1" + " " * N + "x",
    "Imagen 1." + "1" * N,
    "Google " + " " * N,
    "Google" + " Imagen" * (N // 7),
    "DALL" * (N // 4),
    "Affinity " * (N // 9),
]


@pytest.mark.parametrize("marker", markers.TEXT_MARKERS, ids=lambda m: m.key)
def test_text_markers_are_fast(marker):
    for text in TEXT_ADVERSARIAL:
        assert _timed(marker.pattern.search, text) < FAST, text[:20]


@pytest.mark.parametrize("value", [
    "a." * N + "!@example.org",
    "a" * N + "@" + "b" * N + ".com",
    "x@" + "a" + "-" * N + ".com",
    "x@" + "a." * N + "!",
    "x@example." + "xn--" + "a" * N,
])
def test_email_validation_is_fast(value):
    assert _timed(petition.valid_email, value) < FAST
    assert _timed(petition.EMAIL_LOCAL_RE.match, value) < FAST
    assert _timed(petition.EMAIL_LABEL_RE.match, value) < FAST


# The linear A1111 matcher must accept exactly what the old regex accepted (checked on small inputs,
# where the old pattern is still fast).
_OLD_A1111 = re.compile(rb"(?s).+\nSteps:\s*\d+,\s*Sampler:\s*[^,\n]+,.*?CFG scale:\s*[\d.]+,.*?Seed:\s*\d+")
_PARTS = [
    [b"", b"cat", b"a\n"],
    [b"\nSteps:", b"Steps:", b"\nSteps:\n"],
    [b"", b" ", b"\n", b"\t "],
    [b"20", b"", b"x", b"2.5"],
    [b",", b"", b"\n,"],
    [b" ", b"", b"\n "],
    [b"Sampler:", b"sampler:"],
    [b"", b" ", b"  \n ", b"\n", b" \n\n"],
    [b"Euler a", b"", b" ", b"\nx"],
    [b",", b"", b"\n,"],
    [b" ", b"\n", b"junk, ", b"Seed: 1, ", b"CFG scale: x, "],
    [b"CFG scale:", b"CFG Scale:", b""],
    [b" ", b"", b"\n"],
    [b"7", b"7.5", b"", b"."],
    [b",", b"", b" ,"],
    [b" ", b"x, ", b"\n"],
    [b"Seed:", b"seed:", b""],
    [b" ", b"", b"\n\n"],
    [b"1", b"", b"z"],
]


def test_a1111_matcher_equals_old_grammar_on_random_inputs():
    rng = random.Random(1234)
    matched = 0
    for _ in range(30000):
        data = b"".join(part[0] if rng.random() < 0.8 else rng.choice(part) for part in _PARTS)
        expected = _OLD_A1111.search(data) is not None
        matched += expected
        assert (markers._a1111_match(data) is not None) == expected, data
    assert matched > 500  # the corpus does exercise positive cases


@pytest.mark.parametrize("value,expected", [
    (media.SD_PARAMETERS, True),
    ("cat\nSteps: 20, Sampler: Euler a, CFG scale: 7, Seed: 1", True),
    ("cat\nSteps:20,Sampler:Euler,\nmore\nCFG scale:7.5,\nSeed:42", True),
    ("cat\nSteps: 20, Sampler:   \n  Euler, CFG scale: 7, Seed: 1", True),
    ("Steps: 20, Sampler: Euler, CFG scale: 7, Seed: 1", False),  # no prompt before the settings line
    ("cat\nSteps: 20, Sampler: Euler, CFG scale: 7", False),
    ("cat\nSteps: 20, Sampler: Euler, Seed: 1, CFG scale: 7,", False),
    ("cat\nSteps: 20, Sampler: , CFG scale: 7, Seed: 1", True),
    ("cat\nSteps: 20, Sampler:\n, CFG scale: 7, Seed: 1", False),
])
def test_a1111_grammar_examples(value, expected):
    hits = markers.scan_generator_metadata(value.encode(), "PNG text", "parameters")
    assert bool(hits) is expected
    assert bool(_OLD_A1111.search(value.encode())) is expected


@pytest.mark.parametrize("value,expected", [
    ('{"3": {"class_type": "KSampler", "inputs": {"seed": 1}}}', True),
    ('{"3": {"inputs": {"seed": 1}, "class_type": "KSampler"}}', True),
    ('{"class_type": "KSampler"' + " " * 401 + '"inputs"}', False),
    ('{"class_type": "KSampler"' + " " * 400 + '"inputs"}', True),
    ('{"inputs"' + " " * 4001 + '"class_type": 1}', False),
    ('{"inputs"' + " " * 4000 + '"class_type": 1}', True),
    ('{"class_type": "9bad", "x": 1}', False),
])
def test_comfy_grammar_examples(value, expected):
    old = re.compile(rb'"class_type"\s*:\s*"[A-Za-z]+[^"]*".{0,400}?"inputs"|"inputs".{0,4000}?"class_type"\s*:', re.S)
    data = value.encode()
    hits = markers.scan_generator_metadata(data, "PNG text", "prompt")
    assert bool(hits) is expected
    assert bool(old.search(data)) is expected
    if expected:  # the snippet starts where the old regex's match started
        assert hits[0].snippet == markers._snippet(data[old.search(data).start():].decode(), 0)


# --- 2. PNG chunk-table amplification ---------------------------------------------------------------

def _tiny_chunk_png(total: int = 5 * 1024 * 1024) -> bytes:
    """A valid 1x1 PNG with ~`total` bytes of empty ancillary chunks (12 bytes each) before IDAT."""
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), "gray").save(buf, "PNG")
    png = buf.getvalue()
    empty = media.png_chunk(b"prVt", b"")
    return png[:33] + empty * (total // len(empty)) + png[33:]


def test_png_with_millions_of_tiny_chunks_is_bounded(tmp_path):
    path = tmp_path / "chunks.png"
    path.write_bytes(_tiny_chunk_png())
    results = []
    tracemalloc.start()
    try:
        start = time.perf_counter()
        for fn in (exif.extract, exif._png_text_chunks, exif._png_without_text, exif.decode_for_model):
            try:
                results.append(fn(path))
            except Exception as exc:  # noqa: BLE001 - the outcome is asserted below
                results.append(exc)
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 20 * 1024 * 1024, f"peak {peak / 2**20:.1f} MB"
    assert elapsed < 2.0, f"{elapsed:.2f} s"
    meta, texts, stripped, decoded = results
    assert meta.error and "PngChunkLimit" in meta.error  # treated as damaged, never a crash
    assert texts == {}
    assert isinstance(stripped, ValueError) and type(stripped).__name__ == "PngChunkLimit"
    assert isinstance(decoded, exif.DecodeFailed) and "damaged" in decoded.reason


def test_png_chunk_flood_scan_reports_damaged_file(client):
    res = client.post("/api/scan", files={"file": ("chunks.png", _tiny_chunk_png(), "image/png")})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["verdict"] != "ai"
    assert any(s["kind"] == "info" and "could not be decoded" in s["text"] for s in body["signals"])


def test_png_metadata_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(exif, "MAX_PNG_METADATA_BYTES", 1000)
    png = media.insert_png_chunk(media.photo_png(), media.png_chunk(b"prVt", b"x" * 1001))
    path = tmp_path / "big.png"
    path.write_bytes(png)
    assert "metadata" in (exif.png_chunk_problem(path) or "")
    assert exif.extract(path).error


def test_png_chunk_lengths_are_validated_against_file_size(tmp_path):
    png = media.photo_png()
    # A chunk declaring 2 GB of data right before IEND: walking must stop there, not seek and loop.
    bogus = struct.pack(">I4s", 0x7FFFFFF0, b"prVt") + b"\0" * 16
    path = tmp_path / "bogus.png"
    path.write_bytes(media.insert_png_chunk(png, bogus, after_image=True))
    with open(path, "rb") as f:
        chunks = list(exif._png_chunks(f))
    assert chunks[-1][0] == b"prVt"
    assert exif.png_chunk_problem(path) is None
    meta = exif.extract(path)
    assert meta.width == 256


def test_normal_png_text_still_read_after_chunk_changes(tmp_path):
    path = tmp_path / "sd.png"
    path.write_bytes(media.png({"parameters": media.SD_PARAMETERS}, compressed=True))
    assert exif._png_text_chunks(path)["parameters"] == media.SD_PARAMETERS
    stripped = exif._png_without_text(path)
    assert b"iTXt" not in stripped and b"IDAT" in stripped
    bomb = media.insert_png_chunk(media.photo_png(), media.png_chunk(
        b"zTXt", b"Comment\0\0" + zlib.compress(b"a" * (2 * 1024 * 1024))))
    path.write_bytes(bomb)
    assert len(exif._png_text_chunks(path)["Comment"]) == exif.MAX_TEXT


# --- 3. Rate-limiter retention -----------------------------------------------------------------------

SWEEP_INTERVAL = 60.0  # the retention bound promised by app/templates/privacy.html: longest window + 60 s


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.parametrize("window", [60.0, 3600.0])
def test_rate_limiter_forgets_ips_within_window_plus_sweep_interval(window):
    clock = FakeClock()
    lim = ratelimit.RateLimiter(5, window, clock=clock)
    step = 7.0

    def traffic(until: float) -> None:  # other clients keep the limiter busy
        while clock.t < until:
            clock.t += step
            lim.hit(f"203.0.113.{int(clock.t) % 200}")

    # The worst case for a periodic sweep: the IP's last hit lands right after a sweep has run.
    traffic(clock.t + window + 1)
    clock.t += 30
    hit_at = clock.t
    assert lim.hit("198.51.100.7")
    traffic(hit_at + window + SWEEP_INTERVAL + step)  # + step: sweeps only run on a request
    assert "198.51.100.7" not in lim._hits
    assert clock.t - hit_at <= window + SWEEP_INTERVAL + 2 * step


def test_rate_limiter_prunes_key_on_access():
    clock = FakeClock()
    lim = ratelimit.RateLimiter(2, 3600, clock=clock)
    assert lim.hit("a") and lim.hit("a") and not lim.hit("a")
    clock.t += 3600
    assert lim.allowed("a")
    assert "a" not in lim._hits  # expired key dropped on access, not kept until a sweep
    assert lim.hit("a")


def test_rate_limiter_sweep_without_traffic():
    clock = FakeClock()
    lim = ratelimit.RateLimiter(2, 3600, clock=clock)
    lim.hit("a")
    clock.t += 3600 + 1
    lim.sweep()  # what the background sweeper calls every SWEEP_INTERVAL seconds
    assert lim._hits == {}


def test_rate_limiter_still_limits():
    clock = FakeClock()
    lim = ratelimit.RateLimiter(3, 60, clock=clock)
    assert all(lim.hit("k") for _ in range(3))
    assert not lim.hit("k") and not lim.allowed("k")
    clock.t += 59
    assert not lim.hit("k")
    clock.t += 2
    assert lim.hit("k")
    lim.refund("k")
    assert lim.allowed("k")


def test_privacy_page_matches_retention(client):
    html = client.get("/privacy.html").text
    assert "up to about an hour" in html
    assert ratelimit.SWEEP_INTERVAL <= SWEEP_INTERVAL


# --- 4. Admin token strength -------------------------------------------------------------------------

def test_short_admin_token_disables_admin(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("BTF_ADMIN_TOKEN", "s" * 23)
    with caplog.at_level(logging.ERROR, logger="banthefake"):
        settings = Settings.from_env()
    assert settings.admin_token is None
    assert any("BTF_ADMIN_TOKEN" in r.message and r.levelno == logging.ERROR for r in caplog.records)
    client = TestClient(create_app(replace(settings, db_path=tmp_path / "t.db")))
    res = client.get("/api/petition/export.csv", headers={"Authorization": "Bearer " + "s" * 23})
    assert res.status_code == 404


def test_long_admin_token_enabled(monkeypatch, tmp_path):
    token = "x" * 24
    monkeypatch.setenv("BTF_ADMIN_TOKEN", f"  {token}\n")
    settings = Settings.from_env()
    assert settings.admin_token == token
    client = TestClient(create_app(replace(settings, db_path=tmp_path / "t.db")))
    assert client.get("/api/petition/export.csv", headers={"Authorization": f"Bearer {token}"}).status_code == 200
