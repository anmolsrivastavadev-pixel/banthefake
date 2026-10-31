import io
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import ExifTags, Image, PngImagePlugin

from app.petition import PetitionStore
from app.scan import c2pa_check, exif, markers, model, video
from tests import media
from tests.conftest import FIXTURES


def _scan(client, name: str, data: bytes) -> dict:
    response = client.post("/api/scan", files={"file": (name, data, "application/octet-stream")})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.skipif(not c2pa_check.AVAILABLE, reason="c2pa-python not installed")
def test_invalid_c2pa_pixel_hash_is_ignored(client, tmp_path):
    data = bytearray((FIXTURES / "C.jpg").read_bytes())
    data[-100] ^= 1  # pixel scan data after the manifest
    path = tmp_path / "tampered.jpg"
    path.write_bytes(data)
    cred = c2pa_check.read(path, "image/jpeg")
    assert cred and not cred.valid and "assertion.dataHash.mismatch" in cred.failures
    body = _scan(client, "tampered.jpg", bytes(data))
    assert body["verdict"] != "ai"
    assert not any(s["kind"] in {"ai", "edit"} for s in body["signals"])
    assert any("failed validation (assertion.dataHash.mismatch) — ignored" in s["text"] for s in body["signals"])


def test_invalid_c2pa_ai_declaration_cannot_drive_signal():
    from app.scan import _Collector, _c2pa_signals

    cred = c2pa_check.summarize({
        "active_manifest": "x", "validation_status": [{"code": "claimSignature.mismatch"}],
        "manifests": {"x": {"assertions": [{"label": "c2pa.actions.v2", "data": {"actions": [{
            "action": "c2pa.created", "digitalSourceType":
            "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
        }]}}]}}
    }, "Invalid")
    out = _Collector()
    _c2pa_signals(out, cred)
    assert [s["kind"] for s in out.signals] == ["info"]


def test_detector_failure_overrides_camera_reassurance(client, monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(exif, "decode_for_model", lambda path: (_ for _ in ()).throw(exif.DecodeFailed("broken")))
    body = _scan(client, "camera.jpg", media.jpeg({
        ExifTags.Base.Make: "Canon", ExifTags.Base.Model: "Canon EOS R5",
        ExifTags.Base.Software: "Adobe Photoshop 25",
    }))
    assert body["verdict"] == "unknown"
    assert body["verdict_title"] == "Could not fully analyze"


@pytest.fixture(scope="module")
def offset_video(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    path = tmp_path_factory.mktemp("offset") / "offset.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
        "-t", "20", "-g", "600", "-c:v", "libx264", "-output_ts_offset", "100", str(path),
    ], check=True)
    return path


def test_video_timestamps_are_relative_to_stream_start(offset_video):
    info = video.probe(offset_video)
    assert len(info.frames) == video.SAMPLE_FRAMES
    assert 0 <= info.timestamps[0] < 3
    assert info.timestamps[-1] > 17
    assert info.timestamps[-1] <= info.duration_s + 0.2


@pytest.fixture(scope="module")
def piped_webm(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    path = tmp_path_factory.mktemp("pipe") / "pipe.webm"
    command = [
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
        "testsrc2=size=320x180:rate=30:duration=20", "-c:v", "libvpx-vp9", "-deadline", "realtime",
        "-cpu-used", "8", "-g", "60", "-f", "webm", "pipe:1",
    ]
    path.write_bytes(subprocess.check_output(command))
    return path


def test_unknown_duration_video_decodes_forward_and_reports_partial(client, piped_webm, monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "predict", media.fake_predict(0.1))
    info = video.probe(piped_webm)
    assert info.duration_s is None and len(info.frames) >= 3
    assert info.timestamps == sorted(info.timestamps) and info.timestamps[-1] >= 10
    body = _scan(client, "pipe.webm", piped_webm.read_bytes())
    assert any("duration is unknown" in s["text"] and "partial" in s["text"] for s in body["signals"])


def test_low_colour_low_score_stays_unknown_without_reassurance(client, monkeypatch):
    buf = io.BytesIO()
    Image.linear_gradient("L").resize((256, 256)).save(buf, "PNG")
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "predict", media.fake_predict(0.01))
    body = _scan(client, "gray.png", buf.getvalue())
    assert body["verdict"] == "unknown"
    assert any("unreliable on black-and-white or low-colour" in s["text"] for s in body["signals"])
    assert not any("no strong AI signal" in s["text"] for s in body["signals"])


def test_petition_rate_reservation_is_atomic(make_client, monkeypatch):
    real_add = PetitionStore.add
    barrier = threading.Barrier(2)

    def slow_add(self, sig):
        time.sleep(0.1)
        return real_add(self, sig)

    monkeypatch.setattr(PetitionStore, "add", slow_add)
    client = make_client(sign_rate=1)
    payloads = [
        {"name": "One", "email": "one@example.org", "consent": True},
        {"name": "Two", "email": "two@example.org", "consent": True},
    ]

    def post(payload):
        barrier.wait()
        return client.post("/api/petition", json=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(post, payloads))
    assert sorted(statuses) == [201, 429]


def test_petition_reservation_is_refunded_after_rejection(make_client):
    client = make_client(sign_rate=1)
    invalid = {"name": "", "email": "bad@example.org", "consent": True}
    valid = {"name": "Valid", "email": "valid@example.org", "consent": True}
    assert client.post("/api/petition", json=invalid).status_code == 422
    assert client.post("/api/petition", json=valid).status_code == 201


def test_petition_reservation_is_refunded_after_duplicate(make_client):
    client = make_client(sign_rate=2)
    first = {"name": "First", "email": "first@example.org", "consent": True}
    second = {"name": "Second", "email": "second@example.org", "consent": True}
    assert client.post("/api/petition", json=first).status_code == 201
    assert client.post("/api/petition", json=first).status_code == 409
    assert client.post("/api/petition", json=second).status_code == 201


def test_petition_reservation_is_refunded_after_database_failure(make_client, monkeypatch):
    real_add = PetitionStore.add
    calls = 0

    def fail_once(self, sig):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")
        return real_add(self, sig)

    monkeypatch.setattr(PetitionStore, "add", fail_once)
    client = make_client(sign_rate=1)
    first = {"name": "First", "email": "first@example.org", "consent": True}
    second = {"name": "Second", "email": "second@example.org", "consent": True}
    with pytest.raises(RuntimeError, match="database unavailable"):
        client.post("/api/petition", json=first)
    assert client.post("/api/petition", json=second).status_code == 201


@pytest.mark.parametrize("key,value", [
    ("Description", "Steps: 20, Sampler: Euler, CFG scale: 7, Seed: 1"),
    ("Comment", media.SD_PARAMETERS),
    ("parameters", "Midjourney is a photo contest"),
    ("prompt", "a cat"),
])
def test_free_text_generator_words_do_not_count(client, key, value):
    body = _scan(client, "caption.png", media.png({key: value}))
    assert body["verdict"] != "ai"
    assert not any(s["kind"] == "ai" for s in body["signals"])


@pytest.mark.parametrize("software", ["Editor de Imagen 2.1", "Veo Camera 3.2"])
def test_ambiguous_imagen_and_veo_software_are_not_ai(client, software):
    body = _scan(client, "ordinary.jpg", media.jpeg({ExifTags.Base.Software: software}))
    assert body["verdict"] != "ai"


def test_full_a1111_user_comment_counts(client):
    body = _scan(client, "generated.jpg", media.jpeg(exif_ifd={ExifTags.Base.UserComment: media.SD_PARAMETERS}))
    assert body["verdict"] == "ai"
    assert any(s.get("field") == "EXIF UserComment" for s in body["signals"])


def test_truncated_global_flag_does_not_leak_between_decodes(tmp_path):
    from PIL import ImageFile

    png = media.photo_png()
    broken = tmp_path / "broken.png"
    strict = tmp_path / "strict.png"
    broken.write_bytes(png[:png.index(b"IDAT") + 400])
    strict.write_bytes(png)
    entered = threading.Event()
    leaked = []
    original_convert = Image.Image.convert

    def observed_convert(self, *args, **kwargs):
        if threading.current_thread().name == "tolerant":
            entered.set()
            time.sleep(0.2)
        elif threading.current_thread().name == "strict":
            leaked.append(ImageFile.LOAD_TRUNCATED_IMAGES)
        return original_convert(self, *args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Image.Image, "convert", observed_convert)

    def tolerant():
        try:
            exif.decode_for_model(broken)
        except exif.DecodeFailed:
            pass

    def strict_decode():
        assert entered.wait(1)
        exif.decode_for_model(strict)

    a = threading.Thread(target=tolerant, name="tolerant")
    b = threading.Thread(target=strict_decode, name="strict")
    a.start()
    b.start()
    a.join()
    b.join()
    monkeypatch.undo()
    assert leaked and not any(leaked)
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False


def test_less_than_twenty_percent_decoded_is_rejected(tmp_path):
    data = media.photo_jpeg((1024, 1024))
    sos = data.index(b"\xff\xda")
    path = tmp_path / "almost-empty.jpg"
    path.write_bytes(data[:sos + 20])
    with pytest.raises(exif.DecodeFailed, match="less than 20%"):
        exif.decode_for_model(path)


def test_multipart_overlong_boundary_is_400(client):
    boundary = "x" * 100_000
    response = client.post(
        "/api/scan", content=b"", headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Malformed upload."
