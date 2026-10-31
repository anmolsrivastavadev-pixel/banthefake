"""Opt-in experimental face-swap signal.

The runtime is deliberately local-only: YuNet and the classifier checkpoint must already exist on disk.
Both backends load lazily, once, behind locks.  Public analysis functions catch every backend/decode error and
return ``(None, reason)`` so this experimental signal can never break the main scan.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NotRequired, Protocol, TypedDict

import numpy as np
from PIL import Image

log = logging.getLogger("banthefake.faceswap")

DEFAULT_YUNET = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
MIN_FACE = 64
DETECT_THRESHOLD = 0.5
FACE_THRESHOLD = 0.8
DEFAULT_MAX_FACES = 8
DEFAULT_MAX_VIDEO_FACES = 16
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_WEIGHTS_MB = 2048.0
MAX_YUNET_BYTES = 10 * 1024 * 1024
SCORE_BATCH_SIZE = 8
CHECKPOINT_FORMAT = 1

# GenD-style five-point alignment: left/right eye, nose, left/right mouth corner.
_DST = np.array(
    [[0.34, 0.46], [0.66, 0.46], [0.5, 0.64], [0.37, 0.82], [0.63, 0.82]], dtype=np.float32
)

Status = Literal["off", "loaded", "loading", "unavailable"]


class FaceScore(TypedDict):
    box: list[int]
    score: float
    frame: NotRequired[int]
    timestamp_s: NotRequired[float]


class FaceSwapResult(TypedDict):
    status: Literal["ok"]
    experimental: Literal[True]
    faces: list[FaceScore]
    max_score: float | None
    flagged: bool
    threshold: float
    model_version: str
    candidates_detected: int
    faces_skipped: int
    faces_omitted: int
    faces_timed_out: int
    faces_flagged: int
    frames_checked: int
    note: NotRequired[str]


class FaceSwapUnavailable(TypedDict):
    status: Literal["unavailable"]
    experimental: Literal[True]
    reason: str


FaceSwapReport = FaceSwapResult | FaceSwapUnavailable


class DetectorBackend(Protocol):
    def detect(self, rgb: np.ndarray) -> np.ndarray: ...


class ClassifierBackend(Protocol):
    threshold: float
    model_version: str

    def score(self, crops: list[np.ndarray]) -> list[float]: ...


def enabled() -> bool:
    """The feature is enabled only by the explicit value ``BTF_FACESWAP=on``."""
    return os.environ.get("BTF_FACESWAP", "off").strip().lower() == "on"


def weights_path() -> Path | None:
    value = os.environ.get("BTF_FACESWAP_WEIGHTS", "").strip()
    return Path(value).expanduser() if value else None


def yunet_path() -> Path:
    value = os.environ.get("BTF_YUNET_PATH", "").strip()
    return Path(value).expanduser() if value else DEFAULT_YUNET


def max_faces() -> int:
    value = os.environ.get("BTF_FACESWAP_MAX_FACES", "").strip()
    if not value:
        return DEFAULT_MAX_FACES
    if not value.isdigit() or not 1 <= int(value) <= 64:
        raise ValueError("BTF_FACESWAP_MAX_FACES must be an integer from 1 to 64")
    return int(value)


def max_video_faces() -> int:
    value = os.environ.get("BTF_FACESWAP_MAX_VIDEO_FACES", "").strip()
    if not value:
        return DEFAULT_MAX_VIDEO_FACES
    if not value.isdigit() or not 1 <= int(value) <= 256:
        raise ValueError("BTF_FACESWAP_MAX_VIDEO_FACES must be an integer from 1 to 256")
    return int(value)


def timeout_s() -> float:
    value = os.environ.get("BTF_FACESWAP_TIMEOUT_S", "").strip()
    if not value:
        return DEFAULT_TIMEOUT_S
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ValueError("BTF_FACESWAP_TIMEOUT_S must be a positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("BTF_FACESWAP_TIMEOUT_S must be a positive number")
    return timeout


def max_weights_mb() -> float:
    value = os.environ.get("BTF_FACESWAP_MAX_WEIGHTS_MB", "").strip()
    if not value:
        return DEFAULT_MAX_WEIGHTS_MB
    try:
        limit = float(value)
    except ValueError as exc:
        raise ValueError("BTF_FACESWAP_MAX_WEIGHTS_MB must be a positive number") from exc
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("BTF_FACESWAP_MAX_WEIGHTS_MB must be a positive number")
    return limit


def threshold_override() -> float | None:
    value = os.environ.get("BTF_FACESWAP_THRESHOLD", "").strip()
    if not value:
        return None
    try:
        threshold = float(value)
    except ValueError as exc:
        raise ValueError("BTF_FACESWAP_THRESHOLD must be a number from 0 to 1") from exc
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("BTF_FACESWAP_THRESHOLD must be a number from 0 to 1")
    return threshold


def deps_installed() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("onnxruntime", "torch", "transformers"))


class _YuNetORT:
    def __init__(self, path: Path) -> None:
        import onnxruntime as ort

        ort.disable_telemetry_events()
        self._session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        model_input = self._session.get_inputs()[0]
        if model_input.shape != [1, 3, 640, 640]:
            raise ValueError(f"unexpected YuNet input shape: {model_input.shape}")
        self._input_name = model_input.name

    def detect(self, rgb: np.ndarray) -> np.ndarray:
        # YuNet's published ONNX input is fixed at 640x640. Preserve the source aspect ratio and pad the
        # unused dimension; stretching wide and tall images makes otherwise detectable faces disappear.
        detector_image, scale_x, scale_y, pad_x, pad_y = _letterbox(rgb, 640)
        bgr = np.ascontiguousarray(detector_image[..., ::-1].transpose(2, 0, 1)[None], dtype=np.float32)
        outputs = {
            output.name: value
            for output, value in zip(self._session.get_outputs(), self._session.run(None, {self._input_name: bgr}))
        }
        proposals = []
        for stride in (8, 16, 32):
            cls = outputs[f"cls_{stride}"][0, :, 0]
            obj = outputs[f"obj_{stride}"][0, :, 0]
            bbox = outputs[f"bbox_{stride}"][0]
            landmarks = outputs[f"kps_{stride}"][0]
            scores = np.sqrt(np.clip(cls * obj, 0, None))
            cols = 640 // stride
            for index in np.flatnonzero(scores >= DETECT_THRESHOLD):
                col, row = int(index % cols), int(index // cols)
                center_x = (col + float(bbox[index, 0])) * stride
                center_y = (row + float(bbox[index, 1])) * stride
                face_width = math.exp(float(bbox[index, 2])) * stride
                face_height = math.exp(float(bbox[index, 3])) * stride
                values = [center_x - face_width / 2, center_y - face_height / 2, face_width, face_height]
                for point in range(5):
                    values.extend(
                        [
                            (col + float(landmarks[index, point * 2])) * stride,
                            (row + float(landmarks[index, point * 2 + 1])) * stride,
                        ]
                    )
                values.append(float(scores[index]))
                proposals.append(values)
        if not proposals:
            return np.zeros((0, 15), dtype=np.float32)
        faces = np.asarray(proposals, dtype=np.float32)
        faces = faces[_nms(faces, 0.3, 5000)]
        return _coordinates_from_letterbox(faces, scale_x, scale_y, pad_x, pad_y)


def _letterbox(rgb: np.ndarray, size: int) -> tuple[np.ndarray, float, float, int, int]:
    """Scale an RGB image to fit a square detector input without changing its aspect ratio."""
    height, width = rgb.shape[:2]
    if height < 1 or width < 1:
        raise ValueError("face detector received an empty image")
    scale = min(size / width, size / height)
    scaled_width = max(1, min(size, round(width * scale)))
    scaled_height = max(1, min(size, round(height * scale)))
    resized = np.asarray(
        Image.fromarray(rgb, "RGB").resize((scaled_width, scaled_height), Image.Resampling.BILINEAR)
    )
    pad_x = (size - scaled_width) // 2
    pad_y = (size - scaled_height) // 2
    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    canvas[pad_y:pad_y + scaled_height, pad_x:pad_x + scaled_width] = resized
    return canvas, scaled_width / width, scaled_height / height, pad_x, pad_y


def _coordinates_from_letterbox(
    faces: np.ndarray, scale_x: float, scale_y: float, pad_x: int, pad_y: int
) -> np.ndarray:
    """Map YuNet boxes and five landmarks from detector coordinates to source pixels."""
    mapped = faces.copy()
    x_positions = [0, 4, 6, 8, 10, 12]
    y_positions = [1, 5, 7, 9, 11, 13]
    mapped[:, x_positions] = (mapped[:, x_positions] - pad_x) / scale_x
    mapped[:, y_positions] = (mapped[:, y_positions] - pad_y) / scale_y
    mapped[:, 2] /= scale_x
    mapped[:, 3] /= scale_y
    return mapped


def _nms(faces: np.ndarray, iou_threshold: float, top_k: int) -> list[int]:
    """Score-ordered box NMS, matching FaceDetectorYN's default policy."""
    order = np.argsort(-faces[:, 14])[:top_k]
    keep: list[int] = []
    while order.size:
        current = int(order[0])
        keep.append(current)
        if order.size == 1:
            break
        rest = order[1:]
        left = np.maximum(faces[current, 0], faces[rest, 0])
        top = np.maximum(faces[current, 1], faces[rest, 1])
        right = np.minimum(faces[current, 0] + faces[current, 2], faces[rest, 0] + faces[rest, 2])
        bottom = np.minimum(faces[current, 1] + faces[current, 3], faces[rest, 1] + faces[rest, 3])
        intersection = np.maximum(0, right - left) * np.maximum(0, bottom - top)
        area_current = faces[current, 2] * faces[current, 3]
        area_rest = faces[rest, 2] * faces[rest, 3]
        union = area_current + area_rest - intersection
        iou = np.divide(intersection, union, out=np.zeros_like(intersection), where=union > 0)
        order = rest[iou <= iou_threshold]
    return keep


class _CheckpointClassifier:
    """CLIP vision backbone + trained LayerNorm and linear head from one local checkpoint."""

    def __init__(self, path: Path) -> None:
        import torch
        from transformers import CLIPImageProcessor, CLIPVisionConfig, CLIPVisionModel

        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        if not isinstance(checkpoint, dict) or checkpoint.get("format_version") != CHECKPOINT_FORMAT:
            raise ValueError(f"unsupported face-swap checkpoint format (expected {CHECKPOINT_FORMAT})")
        backbone_id = checkpoint.get("backbone_id")
        revision = checkpoint.get("backbone_revision")
        if not isinstance(backbone_id, str) or not backbone_id.startswith("openai/clip-"):
            raise ValueError("checkpoint backbone_id must name an openai/clip vision model")
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError("checkpoint backbone_revision must be pinned")
        metadata_raw = checkpoint.get("metadata_json")
        if not isinstance(metadata_raw, str):
            raise ValueError("checkpoint metadata_json must be a JSON string")
        metadata = json.loads(metadata_raw)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("vision_config"), dict):
            raise ValueError("checkpoint metadata_json must include vision_config")
        state = checkpoint.get("state_dict")
        if not isinstance(state, dict):
            raise ValueError("checkpoint state_dict is missing")

        _validate_vision_config(metadata["vision_config"])
        vision = CLIPVisionModel(CLIPVisionConfig.from_dict(metadata["vision_config"])).eval()
        vision_state = _prefixed_state(state, "vision.")
        missing, unexpected = vision.load_state_dict(vision_state, strict=False)
        missing = [key for key in missing if not key.endswith("position_ids")]
        if missing or unexpected:
            raise ValueError(
                f"checkpoint vision weights do not match: missing={missing[:3]} unexpected={unexpected[:3]}"
            )
        norm_state = _prefixed_state(state, "layer_norm.")
        head_state = _prefixed_state(state, "head.")
        if set(norm_state) != {"weight", "bias"} or set(head_state) != {"weight", "bias"}:
            raise ValueError("checkpoint needs layer_norm and head weight/bias tensors")
        hidden = int(norm_state["weight"].numel())
        outputs = int(head_state["weight"].shape[0])
        if hidden != vision.config.hidden_size or head_state["weight"].ndim != 2:
            raise ValueError("checkpoint head dimensions do not match the CLIP vision backbone")
        layer_norm = torch.nn.LayerNorm(hidden)
        head = torch.nn.Linear(hidden, outputs)
        layer_norm.load_state_dict(norm_state, strict=True)
        head.load_state_dict(head_state, strict=True)
        layer_norm.eval(), head.eval()

        mean = metadata.get("image_mean", [0.48145466, 0.4578275, 0.40821073])
        std = metadata.get("image_std", [0.26862954, 0.26130258, 0.27577711])
        size = metadata.get("image_size", vision.config.image_size)
        if not isinstance(size, int) or isinstance(size, bool) or not 16 <= size <= 448:
            raise ValueError("checkpoint image_size must be an integer from 16 to 448")
        self._processor = CLIPImageProcessor(
            do_resize=True,
            size={"shortest_edge": size},
            do_center_crop=True,
            crop_size={"height": size, "width": size},
            image_mean=mean,
            image_std=std,
        )
        self._torch = torch
        self._vision, self._layer_norm, self._head = vision, layer_norm, head
        self._positive_class = int(metadata.get("positive_class", 1))
        if outputs > 1 and not 0 <= self._positive_class < outputs:
            raise ValueError("checkpoint positive_class is outside the head output range")
        self.threshold = _probability(checkpoint.get("threshold"), "checkpoint threshold")
        version = checkpoint.get("model_version", metadata.get("model_version"))
        if not isinstance(version, str) or not version.strip():
            raise ValueError("checkpoint model_version is missing")
        self.model_version = version.strip()[:128]

    def score(self, crops: list[np.ndarray]) -> list[float]:
        if not crops:
            return []
        torch = self._torch
        pixels = self._processor(
            images=[Image.fromarray(crop, "RGB") for crop in crops], return_tensors="pt"
        )["pixel_values"]
        with torch.inference_mode():
            pooled = self._vision(pixel_values=pixels).pooler_output
            logits = self._head(self._layer_norm(pooled))
            probs = (
                torch.sigmoid(logits[:, 0])
                if logits.shape[-1] == 1
                else logits.softmax(-1)[:, self._positive_class]
            )
        values = [float(value) for value in probs]
        # Drop the batch-sized tensors before the next scoring chunk is prepared.
        del pixels, pooled, logits, probs
        return values


def _prefixed_state(state: dict, prefix: str) -> dict:
    return {str(key).removeprefix(prefix): value for key, value in state.items() if str(key).startswith(prefix)}


def _probability(value, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number from 0 to 1") from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError(f"{name} must be a number from 0 to 1")
    return result


def _validate_vision_config(config: dict) -> None:
    bounds = {
        "hidden_size": (1, 2048),
        "intermediate_size": (1, 8192),
        "num_hidden_layers": (1, 48),
        "num_attention_heads": (1, 64),
        "projection_dim": (1, 2048),
        "image_size": (16, 448),
    }
    for key, (lower, upper) in bounds.items():
        value = config.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
            raise ValueError(f"checkpoint vision_config {key} must be an integer from {lower} to {upper}")
    patch_size = config.get("patch_size")
    if not isinstance(patch_size, int) or isinstance(patch_size, bool) or patch_size not in {14, 16, 32}:
        raise ValueError("checkpoint vision_config patch_size must be one of 14, 16, or 32")
    channels = config.get("num_channels")
    if channels != 3 or isinstance(channels, bool):
        raise ValueError("checkpoint vision_config num_channels must be 3")
    if config["hidden_size"] % config["num_attention_heads"]:
        raise ValueError("checkpoint vision_config hidden_size must be divisible by num_attention_heads")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_paths() -> tuple[Path, Path]:
    # Validate every environment-controlled value before constructing either ML backend.
    max_faces()
    max_video_faces()
    timeout_s()
    threshold_override()
    weights_limit = max_weights_mb()
    weights = weights_path()
    if weights is None:
        raise RuntimeError("BTF_FACESWAP_WEIGHTS is not set")
    if not weights.is_file():
        raise RuntimeError("the configured face-swap checkpoint does not exist")
    if weights.stat().st_size > weights_limit * 1024 * 1024:
        raise RuntimeError(f"the configured face-swap checkpoint exceeds the {weights_limit:g} MB limit")
    detector_path = yunet_path()
    if not detector_path.is_file():
        raise RuntimeError("the configured YuNet model does not exist")
    if detector_path.stat().st_size > MAX_YUNET_BYTES:
        raise RuntimeError("the configured YuNet model exceeds the 10 MB limit")
    if _sha256(detector_path) != YUNET_SHA256:
        raise RuntimeError("the configured YuNet model sha256 does not match the expected model")
    return weights, detector_path


_RELEVANT_ENV = (
    "BTF_FACESWAP_WEIGHTS",
    "BTF_YUNET_PATH",
    "BTF_FACESWAP_THRESHOLD",
    "BTF_FACESWAP_MAX_FACES",
    "BTF_FACESWAP_MAX_VIDEO_FACES",
    "BTF_FACESWAP_TIMEOUT_S",
    "BTF_FACESWAP_MAX_WEIGHTS_MB",
)


def _load_signature() -> tuple:
    env = tuple((name, os.environ.get(name, "")) for name in _RELEVANT_ENV)
    files = []
    for path in (weights_path(), yunet_path()):
        if path is None:
            files.append(None)
            continue
        try:
            stat = path.stat()
            files.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            files.append((str(path), None, None))
    return env, tuple(files)


class _Runtime:
    def __init__(self) -> None:
        self._load_lock = threading.Lock()
        self.infer_lock = threading.Lock()
        self.detector: DetectorBackend | None = None
        self.classifier: ClassifierBackend | None = None
        self.failed = False
        self.failure_reason = ""
        self.failure_signature: tuple | None = None
        self.failure_count = 0
        self.next_retry_at = 0.0
        self.loaded_signature: tuple | None = None

    @property
    def loaded(self) -> bool:
        return self.detector is not None and self.classifier is not None

    def load(self) -> bool:
        signature = _load_signature()
        if self.loaded and (self.loaded_signature is None or self.loaded_signature == signature):
            return True
        if self.failed and self.failure_signature == signature and time.monotonic() < self.next_retry_at:
            return False
        with self._load_lock:
            signature = _load_signature()
            if self.loaded and (self.loaded_signature is None or self.loaded_signature == signature):
                return True
            if self.failed and self.failure_signature == signature and time.monotonic() < self.next_retry_at:
                return False
            try:
                if not deps_installed():
                    raise RuntimeError("the faceswap extra is not installed")
                weights, detector_path = _validated_paths()
                detector = _YuNetORT(detector_path)
                classifier = _CheckpointClassifier(weights)
                self.detector = detector
                self.classifier = classifier  # set last: loaded never exposes a half-built runtime
                self.loaded_signature = signature
                self.failed = False
                self.failure_reason = ""
                self.failure_signature = None
                self.failure_count = 0
                self.next_retry_at = 0.0
                log.info("loaded experimental face-swap model %s", classifier.model_version)
                return True
            except Exception as exc:
                self.detector = None
                self.classifier = None
                if self.failure_signature == signature:
                    self.failure_count += 1
                else:
                    self.failure_count = 1
                self.failed = True
                self.failure_reason = str(exc) or type(exc).__name__
                self.failure_signature = signature
                self.next_retry_at = time.monotonic() + min(300.0, 2.0 ** (self.failure_count - 1))
                log.exception("could not load experimental face-swap backends")
                return False


_RUNTIME = _Runtime()


def status() -> Status:
    if not enabled():
        return "off"
    try:
        _validated_paths()
    except (RuntimeError, ValueError, OSError) as exc:
        log.warning("experimental face-swap configuration unavailable: %s", exc)
        return "unavailable"
    signature = _load_signature()
    if _RUNTIME.loaded and (_RUNTIME.loaded_signature is None or _RUNTIME.loaded_signature == signature):
        return "loaded"
    if not deps_installed():
        return "unavailable"
    if (_RUNTIME.failed and _RUNTIME.failure_signature == signature
            and time.monotonic() < _RUNTIME.next_retry_at):
        return "unavailable"
    return "loading"


def warmup() -> None:
    if status() == "loading":
        threading.Thread(target=_RUNTIME.load, name="btf-faceswap-warmup", daemon=True).start()


def analyze_faces(
    image: Image.Image,
    detector: DetectorBackend | None = None,
    classifier: ClassifierBackend | None = None,
    *,
    time_budget_s: float | None = None,
) -> tuple[FaceSwapResult | None, str | None]:
    """Analyze one image. Optional backends are dependency-injection seams used by tests."""
    return analyze_video_faces(
        [image], detector=detector, classifier=classifier, time_budget_s=time_budget_s
    )


def analyze_video_faces(
    frames: list[Image.Image],
    timestamps: list[float] | None = None,
    detector: DetectorBackend | None = None,
    classifier: ClassifierBackend | None = None,
    *,
    time_budget_s: float | None = None,
) -> tuple[FaceSwapResult | None, str | None]:
    """Detect per sampled frame and score selected crops in bounded classifier batches."""
    if not frames:
        return None, "no decodable frames were available"
    if (detector is None) != (classifier is None):
        return None, "both injected face-swap backends are required"
    if detector is None:
        if not enabled():
            return None, "the experimental face-swap check is off"
        if not _RUNTIME.load():
            return None, _RUNTIME.failure_reason or "the face-swap backends are unavailable"
        detector, classifier = _RUNTIME.detector, _RUNTIME.classifier
    assert detector is not None and classifier is not None
    try:
        lock = _RUNTIME.infer_lock if detector is _RUNTIME.detector else _NullLock()
        with lock:
            return _analyze(frames, timestamps or [], detector, classifier, time_budget_s), None
    except Exception as exc:
        log.exception("experimental face-swap analysis failed")
        return None, f"face-swap analysis failed ({type(exc).__name__})"


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


@dataclass
class _SelectedFace:
    crop: np.ndarray
    score_entry: FaceScore
    area: float


def _analyze(
    frames: list[Image.Image],
    timestamps: list[float],
    detector: DetectorBackend,
    classifier: ClassifierBackend,
    time_budget_s: float | None = None,
) -> FaceSwapResult:
    selected: list[_SelectedFace] = []
    candidates = skipped = omitted = frames_checked = 0
    limit = max_faces()
    budget = timeout_s() if time_budget_s is None else min(timeout_s(), max(0.0, time_budget_s))
    deadline = time.monotonic() + budget
    deadline_hit = False
    for frame_index, image in enumerate(frames):
        if time.monotonic() >= deadline:
            deadline_hit = True
            break
        rgb = np.asarray(image.convert("RGB"))
        rows = np.asarray(detector.detect(rgb), dtype=np.float32)
        frames_checked += 1
        if rows.size == 0:
            rows = np.zeros((0, 15), dtype=np.float32)
        if rows.ndim != 2 or rows.shape[1] < 15:
            raise ValueError("face detector returned an invalid shape")
        candidates += len(rows)
        eligible = []
        for row in rows:
            confidence = float(row[14])
            width, height = float(row[2]), float(row[3])
            if not math.isfinite(confidence) or confidence < FACE_THRESHOLD or min(width, height) < MIN_FACE:
                skipped += 1
                continue
            eligible.append(row)
        eligible.sort(key=lambda row: float(row[2] * row[3]), reverse=True)
        omitted += max(0, len(eligible) - limit)
        for row in eligible[:limit]:
            try:
                crop = _align_face(rgb, row[4:14].reshape(5, 2))
            except Exception:
                skipped += 1
                continue
            box = _box(row, rgb.shape[1], rgb.shape[0])
            entry: FaceScore = {"box": box, "score": 0.0}
            if len(frames) > 1:
                entry["frame"] = frame_index
                if frame_index < len(timestamps):
                    entry["timestamp_s"] = round(float(timestamps[frame_index]), 3)
            selected.append(_SelectedFace(crop, entry, float(row[2] * row[3])))
    selected.sort(key=lambda face: face.area, reverse=True)
    if len(frames) > 1:
        video_limit = max_video_faces()
        omitted += max(0, len(selected) - video_limit)
        selected = selected[:video_limit]
    threshold = threshold_override()
    threshold = classifier.threshold if threshold is None else threshold
    threshold = _probability(threshold, "face-swap threshold")
    scored: list[_SelectedFace] = []
    raw_scores: list[float] = []
    for offset in range(0, len(selected), SCORE_BATCH_SIZE):
        if time.monotonic() >= deadline:
            deadline_hit = True
            break
        chunk = selected[offset:offset + SCORE_BATCH_SIZE]
        chunk_crops = [face.crop for face in chunk]
        try:
            scores = classifier.score(chunk_crops)
        finally:
            del chunk_crops
        if len(scores) != len(chunk):
            raise ValueError("face classifier returned the wrong number of scores")
        for face, score in zip(chunk, scores, strict=True):
            raw = _probability(score, "face-swap score")
            face.score_entry["score"] = round(raw, 4)
            scored.append(face)
            raw_scores.append(raw)
    timed_out = len(selected) - len(scored)
    if timed_out:
        deadline_hit = True
    face_scores = [face.score_entry for face in scored]
    maximum_raw = max(raw_scores, default=None)
    result: FaceSwapResult = {
        "status": "ok",
        "experimental": True,
        "faces": face_scores,
        "max_score": round(maximum_raw, 4) if maximum_raw is not None else None,
        "flagged": any(score >= threshold for score in raw_scores),
        "threshold": round(threshold, 4),
        "model_version": str(classifier.model_version)[:128],
        "candidates_detected": candidates,
        "faces_skipped": skipped,
        "faces_omitted": omitted,
        "faces_timed_out": timed_out,
        "faces_flagged": sum(score >= threshold for score in raw_scores),
        "frames_checked": frames_checked,
    }
    if deadline_hit:
        result["note"] = "The face-swap inference deadline was reached; remaining faces were not scored."
    return result


def _align_face(rgb: np.ndarray, landmarks: np.ndarray, scale: float = 1.3) -> np.ndarray:
    """Copy of the five-landmark crop used by the research evaluator and GenD."""
    dst = _DST.copy()
    desired = np.linalg.norm(landmarks[:, None, :] - landmarks[None, :, :], axis=-1)
    reference = np.linalg.norm(dst[:, None, :] - dst[None, :, :], axis=-1)
    upper = np.triu_indices(len(dst), k=1)
    size = max(16, int(np.round(np.mean(desired[upper] / reference[upper]) * scale)))
    target = (size, size)
    dst[:, 0] *= target[0]
    dst[:, 1] *= target[1]
    margin = scale - 1
    x_margin, y_margin = target[0] * margin / 2, target[1] * margin / 2
    dst[:, 0] = (dst[:, 0] + x_margin) * target[0] / (target[0] + 2 * x_margin)
    dst[:, 1] = (dst[:, 1] + y_margin) * target[1] / (target[1] + 2 * y_margin)
    # Least-squares similarity transform (source -> destination):
    # u = a*x - b*y + tx; v = b*x + a*y + ty.
    source = landmarks.astype(np.float64)
    design = np.zeros((10, 4), dtype=np.float64)
    values = dst.astype(np.float64).reshape(-1)
    for index, (x, y) in enumerate(source):
        design[index * 2] = [x, -y, 1, 0]
        design[index * 2 + 1] = [y, x, 0, 1]
    (a, b, tx, ty), _, rank, _ = np.linalg.lstsq(design, values, rcond=None)
    if rank < 4 or not np.isfinite([a, b, tx, ty]).all():
        raise ValueError("face landmarks could not be aligned")
    forward = np.array([[a, -b, tx], [b, a, ty], [0, 0, 1]], dtype=np.float64)
    inverse = np.linalg.inv(forward)
    coefficients = tuple(float(value) for value in inverse[:2].reshape(-1))
    crop = Image.fromarray(rgb, "RGB").transform(
        target, Image.Transform.AFFINE, coefficients, resample=Image.Resampling.BILINEAR
    )
    return np.asarray(crop)


def _box(row: np.ndarray, image_width: int, image_height: int) -> list[int]:
    left = max(0, min(image_width, round(float(row[0]))))
    top = max(0, min(image_height, round(float(row[1]))))
    right = max(0, min(image_width, round(float(row[0] + row[2]))))
    bottom = max(0, min(image_height, round(float(row[1] + row[3]))))
    return [left, top, max(0, right - left), max(0, bottom - top)]
