"""Experimental face-swap integration. All routine tests inject tiny fake backends."""

from __future__ import annotations

import os
import re
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.scan import c2pa_check, exif, faceswap, model
from tests import media


def face_row(size: float = 96, confidence: float = 0.95) -> np.ndarray:
    x = y = 20.0
    landmarks = [42, 54, 74, 54, 58, 72, 45, 92, 71, 92]
    return np.array([x, y, size, size, *landmarks, confidence], dtype=np.float32)


class FakeDetector:
    def __init__(self, rows=None) -> None:
        self.rows = np.asarray(rows if rows is not None else [face_row()], dtype=np.float32)

    def detect(self, rgb):
        return self.rows.copy()


class FakeClassifier:
    threshold = 0.8
    model_version = "fake-checkpoint-v1"

    def __init__(self, scores=None, error: Exception | None = None) -> None:
        self.scores = scores
        self.error = error

    def score(self, crops):
        if self.error:
            raise self.error
        if self.scores is None:
            return [0.9] * len(crops)
        assert len(self.scores) == len(crops)
        return list(self.scores)


@pytest.fixture
def install_backends(monkeypatch):
    def install(detector=None, classifier=None):
        monkeypatch.setenv("BTF_FACESWAP", "on")
        monkeypatch.setattr(faceswap._RUNTIME, "detector", detector or FakeDetector())
        monkeypatch.setattr(faceswap._RUNTIME, "classifier", classifier or FakeClassifier())
        monkeypatch.setattr(faceswap._RUNTIME, "failed", False)
        monkeypatch.setattr(faceswap._RUNTIME, "failure_reason", "")

    return install


def post(client, data, name="face.png"):
    response = client.post("/api/scan", files={"file": (name, data)})
    assert response.status_code == 200, response.text
    return response.json()


def face_signal(report):
    return next(signal["text"] for signal in report["signals"] if "experimental face-swap" in signal["text"])


def test_off_by_default_has_no_report_block(client):
    report = post(client, media.photo_png())
    assert "faceswap" not in report
    assert not any("face-swap" in signal["text"] for signal in report["signals"])


def test_isolated_worker_frames_reach_faceswap_without_changing_verdict(
    client, monkeypatch, install_backends
):
    """The parent analyzes only RGB frames returned by an unmodified isolated parser worker."""
    monkeypatch.delenv("BTF_SCAN_ISOLATION", raising=False)
    monkeypatch.setenv("BTF_MODEL", "off")
    assert model.enabled() is False

    monkeypatch.setenv("BTF_FACESWAP", "off")
    baseline = post(client, media.photo_png(), "isolated.png")

    install_backends(classifier=FakeClassifier([0.91]))
    analyzed = []
    budgets = []
    original_analyze = faceswap.analyze_faces

    def record_frame(image, *args, **kwargs):
        analyzed.append((image.mode, image.size))
        budgets.append(kwargs["time_budget_s"])
        return original_analyze(image, *args, **kwargs)

    monkeypatch.setattr(faceswap, "analyze_faces", record_frame)
    report = post(client, media.photo_png(), "isolated.png")

    assert analyzed == [("RGB", (256, 192))]
    assert len(budgets) == 1 and 0 < budgets[0] <= faceswap.timeout_s()
    assert report["faceswap"]["flagged"] is True
    assert (report["verdict"], report["verdict_title"]) == (
        baseline["verdict"], baseline["verdict_title"]
    )


@pytest.mark.parametrize(
    ("score", "flagged", "wording"),
    [
        (0.91, True, "flagged 1 face(s) as possibly manipulated"),
        (0.21, False, "scored 1 face(s) below its threshold"),
    ],
)
def test_flagged_and_not_flagged(client, install_backends, score, flagged, wording):
    install_backends(classifier=FakeClassifier([score]))
    report = post(client, media.photo_png())
    result = report["faceswap"]
    assert result["status"] == "ok" and result["flagged"] is flagged
    assert result["faces"] == [{"box": [20, 20, 96, 96], "score": score}]
    assert result["max_score"] == score and result["threshold"] == 0.8
    assert wording in face_signal(report)
    assert "hint, not evidence" in face_signal(report) if flagged else "says nothing about whether the image is real" in face_signal(report)


def test_no_faces(client, install_backends):
    install_backends(detector=FakeDetector(np.zeros((0, 15), dtype=np.float32)))
    report = post(client, media.photo_png())
    assert report["faceswap"]["faces"] == []
    assert report["faceswap"]["candidates_detected"] == 0
    assert "found no faces to analyze" in face_signal(report)


@pytest.mark.parametrize("row", [face_row(size=63), face_row(confidence=0.79)])
def test_small_or_low_confidence_face_is_skipped(client, install_backends, row):
    install_backends(detector=FakeDetector([row]))
    report = post(client, media.photo_png())
    result = report["faceswap"]
    assert result["faces"] == [] and result["faces_skipped"] == 1
    assert "none were large and confident enough" in face_signal(report)
    assert "smaller than 64 px" in face_signal(report)


def test_backend_failure_is_reported_and_never_raises(client, install_backends):
    install_backends(classifier=FakeClassifier(error=RuntimeError("backend exploded")))
    report = post(client, media.photo_png())
    assert report["faceswap"]["status"] == "unavailable"
    assert "face-swap analysis failed" in report["faceswap"]["reason"]
    assert "could not run" in face_signal(report)


def _credential(kind: str) -> c2pa_check.C2PAResult:
    base = dict(present=True, valid=True, trusted=True, signer="Test signer")
    if kind == "capture":
        return c2pa_check.C2PAResult(**base, capture=True)
    if kind == "capture_edits":
        return c2pa_check.C2PAResult(**base, capture=True, actions=["c2pa.edited"])
    if kind == "ai":
        return c2pa_check.C2PAResult(
            **base,
            ai_generated=True,
            source_types=["http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"],
        )
    return c2pa_check.C2PAResult(**base, ai_generated=True, ingredient_ai=True, ingredients=[{"title": "source"}])


@pytest.mark.parametrize("low_colour", [False, True], ids=["colour", "grayscale"])
@pytest.mark.parametrize(
    ("path", "data", "credential", "scores", "model_error"),
    [
        ("verified-capture", media.photo_png(), "capture", None, False),
        ("verified-capture-edited", media.photo_png(), "capture_edits", None, False),
        ("verified-ai", media.photo_png(), "ai", None, False),
        ("verified-ai-ingredient", media.photo_png(), "ingredient_ai", None, False),
        ("ai-marker", media.png({"parameters": media.SD_PARAMETERS}), None, None, False),
        ("likely-model", media.photo_png(), None, (0.999, 0.999), False),
        ("detector-failed", media.photo_png(), None, (0.1, 0.1), True),
        ("edited", media.photoshop_jpeg(), None, None, False),
        ("low-colour", media.photo_png(), None, (0.1, 0.1), False),
        ("possible-model", media.photo_png(), None, (0.999, 0.01), False),
        ("camera", media.canon_jpeg(), None, None, False),
        ("unknown", media.photo_png(), None, None, False),
    ],
)
def test_all_verdict_paths_are_identical_with_faceswap_on_or_off(
    client, monkeypatch, install_backends, low_colour, path, data, credential, scores, model_error
):
    """Every return path in _verdict is invariant across the face-swap feature flag."""
    monkeypatch.setattr(c2pa_check, "read", lambda *_: _credential(credential) if credential else None)
    original_decode = exif.decode_for_model

    def decode_with_colour(*args, **kwargs):
        return replace(original_decode(*args, **kwargs), low_colour=low_colour)

    monkeypatch.setattr(exif, "decode_for_model", decode_with_colour)
    if scores is not None:
        monkeypatch.setenv("BTF_MODEL", "auto")
        if model_error:
            def fail(_images):
                raise RuntimeError("fake model failure")
            monkeypatch.setattr(model, "predict", fail)
        else:
            monkeypatch.setattr(model, "predict", media.fake_predict(*scores))

    monkeypatch.setenv("BTF_FACESWAP", "off")
    baseline = post(client, data, f"{path}.png")
    install_backends(classifier=FakeClassifier([0.99]))
    report = post(client, data, f"{path}.png")
    assert (baseline["verdict"], baseline["verdict_title"]) == (report["verdict"], report["verdict_title"])
    assert report["faceswap"]["flagged"] is True


def test_signal_does_not_change_verified_ai_verdict(client, monkeypatch, install_backends):
    credential = c2pa_check.summarize(
        {
            "active_manifest": "m",
            "manifests": {"m": {"claim_generator": "Example", "signature_info": {"issuer": "Trusted"},
                "assertions": [{"label": "c2pa.actions", "data": {"actions": [{
                    "action": "c2pa.created",
                    "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia",
                }]}}]}},
        },
        "Trusted",
    )
    monkeypatch.setattr(c2pa_check, "read", lambda path, mime: credential)
    monkeypatch.setenv("BTF_FACESWAP", "off")
    baseline = post(client, media.photo_png())
    install_backends(classifier=FakeClassifier([0.99]))
    report = post(client, media.photo_png())
    assert baseline["verdict"] == report["verdict"] == "verified_ai"
    assert baseline["verdict_title"] == report["verdict_title"]


def test_video_batches_faces_and_aggregates_max_score(monkeypatch):
    monkeypatch.setenv("BTF_FACESWAP_MAX_FACES", "8")
    frames = [Image.new("RGB", (180, 180), "white"), Image.new("RGB", (180, 180), "grey")]
    result, reason = faceswap.analyze_video_faces(
        frames,
        [0.25, 1.75],
        detector=FakeDetector(),
        classifier=FakeClassifier([0.25, 0.92]),
    )
    assert reason is None and result is not None
    assert result["frames_checked"] == 2 and result["max_score"] == 0.92 and result["flagged"] is True
    assert [face["frame"] for face in result["faces"]] == [0, 1]
    assert [face["timestamp_s"] for face in result["faces"]] == [0.25, 1.75]


class CountingClassifier(FakeClassifier):
    def __init__(self, delay=0.0):
        super().__init__()
        self.delay = delay
        self.calls = []

    def score(self, crops):
        assert len(crops) <= faceswap.SCORE_BATCH_SIZE
        self.calls.append(len(crops))
        if self.delay:
            time.sleep(self.delay)
        return [0.5] * len(crops)


@pytest.mark.parametrize("kind", ["image", "video"])
def test_image_and_video_classifier_batches_never_exceed_eight(monkeypatch, kind):
    monkeypatch.setenv("BTF_FACESWAP_MAX_FACES", "16")
    monkeypatch.setenv("BTF_FACESWAP_MAX_VIDEO_FACES", "32")
    rows = [face_row(size=80 + index) for index in range(16)]
    frames = [Image.new("RGB", (256, 256), "white")]
    if kind == "video":
        frames.append(Image.new("RGB", (256, 256), "grey"))
    classifier = CountingClassifier()
    result, reason = faceswap.analyze_video_faces(
        frames, detector=FakeDetector(rows), classifier=classifier
    )
    assert reason is None and result is not None
    assert classifier.calls == ([8, 8] if kind == "image" else [8, 8, 8, 8])


class _Output:
    def __init__(self, name):
        self.name = name


class AspectCheckingSession:
    def get_outputs(self):
        return [
            _Output(f"{kind}_{stride}")
            for stride in (8, 16, 32)
            for kind in ("cls", "obj", "bbox", "kps")
        ]

    def run(self, _outputs, inputs):
        tensor = next(iter(inputs.values()))
        content = np.any(tensor[0] != 0, axis=0)
        ys, xs = np.where(content)
        assert np.ptp(xs) + 1 == 640
        assert np.ptp(ys) + 1 == 240
        assert (np.ptp(xs) + 1) / (np.ptp(ys) + 1) == pytest.approx(1600 / 600)
        outputs = []
        for stride in (8, 16, 32):
            cells = (640 // stride) ** 2
            outputs.extend([
                np.zeros((1, cells, 1), dtype=np.float32),
                np.zeros((1, cells, 1), dtype=np.float32),
                np.zeros((1, cells, 4), dtype=np.float32),
                np.zeros((1, cells, 10), dtype=np.float32),
            ])
        return outputs


def test_yunet_letterboxes_wide_images_without_distorting_content():
    detector = object.__new__(faceswap._YuNetORT)
    detector._session = AspectCheckingSession()
    detector._input_name = "input"
    image = np.full((600, 1600, 3), 255, dtype=np.uint8)
    assert detector.detect(image).shape == (0, 15)


def test_letterbox_coordinate_round_trip_for_box_and_landmarks():
    source = np.array([[320, 120, 400, 200, 400, 180, 620, 180, 510, 220,
                        430, 280, 590, 280, 0.95]], dtype=np.float32)
    detector_coordinates = source.copy()
    scale_x, scale_y, pad_x, pad_y = 0.4, 0.4, 0, 200
    detector_coordinates[:, [0, 4, 6, 8, 10, 12]] = (
        detector_coordinates[:, [0, 4, 6, 8, 10, 12]] * scale_x + pad_x
    )
    detector_coordinates[:, [1, 5, 7, 9, 11, 13]] = (
        detector_coordinates[:, [1, 5, 7, 9, 11, 13]] * scale_y + pad_y
    )
    detector_coordinates[:, 2] *= scale_x
    detector_coordinates[:, 3] *= scale_y
    restored = faceswap._coordinates_from_letterbox(
        detector_coordinates, scale_x, scale_y, pad_x, pad_y
    )
    np.testing.assert_allclose(restored, source, rtol=0, atol=1e-5)


def test_video_face_cap_is_scan_wide_largest_first_and_scores_in_chunks(monkeypatch):
    monkeypatch.setenv("BTF_FACESWAP_MAX_FACES", "10")
    monkeypatch.setenv("BTF_FACESWAP_MAX_VIDEO_FACES", "16")
    rows = [face_row(size=80 + index) for index in range(10)]
    frames = [Image.new("RGB", (256, 256), "white") for _ in range(2)]
    classifier = CountingClassifier()
    result, reason = faceswap.analyze_video_faces(frames, detector=FakeDetector(rows), classifier=classifier)
    assert reason is None and result is not None
    assert len(result["faces"]) == 16 and result["faces_omitted"] == 4
    assert classifier.calls == [8, 8]
    widths = [face["box"][2] for face in result["faces"]]
    assert widths == sorted(widths, reverse=True)


def test_video_inference_deadline_skips_remaining_chunks_with_note(monkeypatch):
    monkeypatch.setenv("BTF_FACESWAP_MAX_FACES", "16")
    monkeypatch.setenv("BTF_FACESWAP_MAX_VIDEO_FACES", "16")
    monkeypatch.setenv("BTF_FACESWAP_TIMEOUT_S", "0.1")
    classifier = CountingClassifier(delay=0.12)
    rows = [face_row(size=80 + index) for index in range(10)]
    result, reason = faceswap.analyze_video_faces(
        [Image.new("RGB", (256, 256), "white"), Image.new("RGB", (256, 256), "grey")],
        detector=FakeDetector(rows),
        classifier=classifier,
    )
    assert reason is None and result is not None
    assert classifier.calls == [8]
    assert len(result["faces"]) == 8 and result["faces_timed_out"] == 8
    assert "deadline" in result["note"]


@pytest.mark.parametrize(
    ("score", "threshold", "flagged"),
    [(0.80003, 0.80004, False), (0.80004, 0.80003, True)],
)
def test_flagged_uses_raw_values_before_four_decimal_serialisation(monkeypatch, score, threshold, flagged):
    classifier = FakeClassifier([score])
    classifier.threshold = threshold
    result, reason = faceswap.analyze_faces(
        Image.new("RGB", (180, 180), "white"), detector=FakeDetector(), classifier=classifier
    )
    assert reason is None and result is not None
    assert result["faces"][0]["score"] == result["threshold"] == 0.8
    assert result["flagged"] is flagged
    assert result["faces_flagged"] == int(flagged)


def test_box_clamps_both_corners_before_calculating_size():
    row = face_row()
    row[0], row[2] = -50, 100
    assert faceswap._box(row, 200, 200) == [0, 20, 50, 96]


@pytest.mark.parametrize("state", ["off", "loaded", "loading", "unavailable"])
def test_health_exposes_every_faceswap_state(client, monkeypatch, state):
    monkeypatch.setattr(faceswap, "status", lambda: state)
    assert client.get("/api/health").json()["faceswap"] == state


def test_frontend_faceswap_contract(client):
    js = client.get("/app.js").text
    css = client.get("/styles.css").text
    assert "r.faceswap" in js and "faceSwapBlock" in js
    assert "Experimental" in js and "manipulation score" in js
    assert "Below threshold — this check misses many face swaps." in js
    assert "No faces were flagged" not in js
    assert "experimental-badge" in css and "faceswap-block" in css
    assert "innerHTML" not in js and not re.search(r"\son[a-z]+\s*=", js)


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        ("BTF_FACESWAP_THRESHOLD", "nan", "threshold"),
        ("BTF_FACESWAP_MAX_FACES", "0", "MAX_FACES"),
        ("BTF_FACESWAP_MAX_VIDEO_FACES", "0", "MAX_VIDEO_FACES"),
    ],
)
def test_health_never_reports_loaded_with_invalid_config(monkeypatch, caplog, name, value, reason):
    runtime = faceswap._Runtime()
    runtime.detector = FakeDetector()
    runtime.classifier = FakeClassifier()
    monkeypatch.setattr(faceswap, "_RUNTIME", runtime)
    monkeypatch.setenv("BTF_FACESWAP", "on")
    monkeypatch.setenv(name, value)
    with caplog.at_level("WARNING", logger="banthefake.faceswap"):
        assert faceswap.status() == "unavailable"
    assert reason.lower() in caplog.text.lower()


def _valid_artifact_config(monkeypatch, tmp_path):
    weights = tmp_path / "weights.pt"
    yunet = tmp_path / "yunet.onnx"
    weights.write_bytes(b"checkpoint")
    yunet.write_bytes(b"yunet")
    monkeypatch.setenv("BTF_FACESWAP", "on")
    monkeypatch.setenv("BTF_FACESWAP_WEIGHTS", str(weights))
    monkeypatch.setenv("BTF_YUNET_PATH", str(yunet))
    monkeypatch.setattr(faceswap, "YUNET_SHA256", faceswap.hashlib.sha256(b"yunet").hexdigest())
    return weights, yunet


@pytest.mark.parametrize("change", ["weights-file", "environment"])
def test_failed_load_retries_immediately_when_artifact_or_environment_changes(monkeypatch, tmp_path, change):
    weights, _ = _valid_artifact_config(monkeypatch, tmp_path)
    monkeypatch.setattr(faceswap, "deps_installed", lambda: True)
    calls = []

    def detector(path):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("temporary load failure")
        return FakeDetector()

    monkeypatch.setattr(faceswap, "_YuNetORT", detector)
    monkeypatch.setattr(faceswap, "_CheckpointClassifier", lambda path: FakeClassifier())
    runtime = faceswap._Runtime()
    assert runtime.load() is False
    assert runtime.load() is False and len(calls) == 1  # unchanged failures observe backoff
    if change == "weights-file":
        weights.write_bytes(b"checkpoint changed")
    else:
        monkeypatch.setenv("BTF_FACESWAP_THRESHOLD", "0.7")
    assert runtime.load() is True and len(calls) == 2


def test_failed_load_uses_exponential_backoff_capped_at_five_minutes(monkeypatch, tmp_path):
    _valid_artifact_config(monkeypatch, tmp_path)
    monkeypatch.setattr(faceswap, "deps_installed", lambda: True)
    monkeypatch.setattr(faceswap, "_YuNetORT", lambda path: (_ for _ in ()).throw(RuntimeError("still failing")))
    now = [100.0]
    monkeypatch.setattr(faceswap.time, "monotonic", lambda: now[0])
    runtime = faceswap._Runtime()
    for attempt in range(10):
        assert runtime.load() is False
        expected = min(300.0, 2.0 ** attempt)
        assert runtime.next_retry_at - now[0] == expected
        assert runtime.load() is False
        now[0] += expected


def test_weights_file_size_cap_is_checked_before_backend_construction(monkeypatch, tmp_path):
    _valid_artifact_config(monkeypatch, tmp_path)
    monkeypatch.setenv("BTF_FACESWAP_MAX_WEIGHTS_MB", "0.000001")
    with pytest.raises(RuntimeError, match="checkpoint exceeds"):
        faceswap._validated_paths()


def test_yunet_size_cap_is_ten_mb(monkeypatch, tmp_path):
    _, yunet = _valid_artifact_config(monkeypatch, tmp_path)
    yunet.write_bytes(b"x" * (faceswap.MAX_YUNET_BYTES + 1))
    monkeypatch.setattr(faceswap, "_sha256", lambda path: faceswap.YUNET_SHA256)
    with pytest.raises(RuntimeError, match="YuNet model exceeds the 10 MB limit"):
        faceswap._validated_paths()


def test_yunet_sha256_is_verified_before_onnx_session(monkeypatch, tmp_path):
    _valid_artifact_config(monkeypatch, tmp_path)
    monkeypatch.setattr(faceswap, "YUNET_SHA256", "0" * 64)
    monkeypatch.setattr(faceswap, "deps_installed", lambda: True)
    constructed = []
    monkeypatch.setattr(faceswap, "_YuNetORT", lambda path: constructed.append(path))
    runtime = faceswap._Runtime()
    assert runtime.load() is False
    assert "sha256 does not match" in runtime.failure_reason
    assert constructed == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("hidden_size", 2049),
        ("intermediate_size", 8193),
        ("num_hidden_layers", 49),
        ("num_attention_heads", 65),
        ("projection_dim", 2049),
        ("image_size", 449),
        ("patch_size", 15),
        ("num_channels", 4),
    ],
)
def test_vision_config_resource_bounds_are_checked_before_model_creation(key, value):
    config = {
        "hidden_size": 768,
        "intermediate_size": 3072,
        "num_hidden_layers": 12,
        "num_attention_heads": 12,
        "projection_dim": 512,
        "image_size": 224,
        "patch_size": 16,
        "num_channels": 3,
    }
    config[key] = value
    with pytest.raises(ValueError, match=key):
        faceswap._validate_vision_config(config)


@pytest.mark.faceswap_ml
def test_real_checkpoint_loads_when_configured():
    value = os.environ.get("BTF_FACESWAP_WEIGHTS", "")
    path = Path(value).expanduser() if value else None
    if path is None or not path.is_file():
        pytest.skip("BTF_FACESWAP_WEIGHTS does not point to an existing checkpoint")
    classifier = faceswap._CheckpointClassifier(path)
    assert classifier.model_version and 0 <= classifier.threshold <= 1
