"""Optional AI-image detectors: two pinned classifiers combined by an agreement rule.

Needs the `ml` extra (torch + transformers). Each model is lazy-loaded once (thread-safe) and runs on CPU in
batches. Disabled with BTF_MODEL=off.

Models (both output P(AI-generated) per image):
- primary "H": haywoodsloan/ai-image-detector-deploy (SwinV2, two-class artificial/real head).
  Override with BTF_MODEL_ID / BTF_MODEL_REVISION.
- secondary "C": buildborderless/CommunityForensics-DeepfakeDet-ViT (ViT-S, single logit).
  Override with BTF_MODEL2_ID / BTF_MODEL2_REVISION.

Decision (`decide`), chosen on the calibration split of the benchmark in `docs/DETECTOR-BENCHMARK.md`:
- "likely":   H >= 0.98 AND C >= 0.05   (both models must agree)
- "possible": H >= 0.98 OR  C >= 0.5
- "none":     otherwise
If one model cannot load, the other still runs but its result is capped at "possible": a single model never
produces "likely" in agreement mode. BTF_MODEL_ENSEMBLE=off runs C alone with its previous production
thresholds (likely >= 0.85, possible >= 0.5).

Weights are fetched from the Hugging Face Hub on first use (HF_HOME sets the cache, HF_HUB_OFFLINE=1 forbids
downloads). Custom model ids get no default revision: set the matching *_REVISION for reproducible deploys.
"""

import importlib.util
import logging
import os
import threading
from dataclasses import dataclass
from typing import Literal

from PIL import Image

log = logging.getLogger("banthefake.model")

PRIMARY_ID = "haywoodsloan/ai-image-detector-deploy"
PRIMARY_REVISION = "4ae1822603cb1ec980c217cfde1ef1b8e0477a16"
SECONDARY_ID = "buildborderless/CommunityForensics-DeepfakeDet-ViT"
SECONDARY_REVISION = "ac6ee457bea904a373065754107451793b56db00"
DEFAULT_MODEL_ID, DEFAULT_MODEL_REVISION = PRIMARY_ID, PRIMARY_REVISION

# Agreement mode (both models). Thresholds were chosen on the benchmark's calibration split only.
H_LIKELY = 0.98  # primary score needed for "likely" (and, on its own, for "possible")
C_AGREE = 0.05  # secondary must at least not reject the image for "likely"
C_POSSIBLE = 0.5  # secondary score that on its own gives "possible"
# BTF_MODEL_ENSEMBLE=off: the secondary model alone, with its previous production thresholds.
C_LIKELY_ALONE = 0.85
BATCH = 8

_OFF = ("off", "0", "false", "no")

Status = Literal["loaded", "loading", "off", "unavailable"]
Tier = Literal["likely", "possible", "none"]
TIER_RANK: dict[Tier, int] = {"none": 0, "possible": 1, "likely": 2}


class _Detector:
    """One image classifier, loaded lazily and at most once."""

    def __init__(self, default_id: str, default_revision: str, id_env: str, revision_env: str) -> None:
        self.default_id, self.default_revision = default_id, default_revision
        self.id_env, self.revision_env = id_env, revision_env
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._model = None
        self._processor = None
        self._positive = 1
        self.failed = False

    def model_id(self) -> str:
        return os.environ.get(self.id_env, "").strip() or self.default_id

    def revision(self) -> str | None:
        override = os.environ.get(self.revision_env, "").strip()
        if override:
            return override
        return self.default_revision if self.model_id() == self.default_id else None

    def short_name(self) -> str:
        return self.model_id().rsplit("/", 1)[-1]

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> bool:
        """Load once. Returns True when ready."""
        if self._model is not None:
            return True
        if self.failed or not deps_installed():
            return False
        with self._load_lock:
            if self._model is not None:
                return True
            if self.failed:
                return False
            try:
                from transformers import (
                    AutoImageProcessor,
                    AutoModelForImageClassification,
                )

                _set_threads()
                mid, revision = self.model_id(), self.revision()
                options = {"revision": revision} if revision else {}
                processor = AutoImageProcessor.from_pretrained(mid, **options)
                net = AutoModelForImageClassification.from_pretrained(mid, use_safetensors=True, **options).eval()
                self._positive = _fake_index(net.config.id2label)
                self._processor = processor
                self._model = net  # set last: `loaded` must never see a half-initialised detector
                log.info("loaded model %s at revision %s", mid, revision or "default")
                return True
            except Exception:
                log.exception("could not load detector model %s", self.model_id())
                self.failed = True
                return False

    def predict(self, images: list[Image.Image]) -> list[float] | None:
        """P(AI-generated) per image, or None if this model could not be loaded."""
        if not self.load():
            return None
        import torch

        out: list[float] = []
        with self._infer_lock, torch.inference_mode():
            for i in range(0, len(images), BATCH):
                batch = [im.convert("RGB") for im in images[i : i + BATCH]]
                logits = self._model(**self._processor(batch, return_tensors="pt")).logits
                if logits.shape[-1] == 1:
                    probs = torch.sigmoid(logits[:, 0])
                else:
                    probs = logits.softmax(-1)[:, self._positive]
                out.extend(float(p) for p in probs)
        return out


def _fake_index(id2label: dict) -> int:
    """Index of the AI/fake class in a two-class head (label names vary between models)."""
    labels = {int(k): str(v).lower() for k, v in id2label.items()}
    for words in (("fake", "artificial", "synthetic", "generated"), ("ai",)):
        for i, name in sorted(labels.items()):
            if any(w in name for w in words):
                return i
    return 1


_threads_set = False


def _set_threads() -> None:
    global _threads_set
    if _threads_set:
        return
    threads = os.environ.get("BTF_MODEL_THREADS", "")
    if threads.isdigit() and int(threads) > 0:
        import torch

        torch.set_num_threads(int(threads))
    _threads_set = True


PRIMARY = _Detector(PRIMARY_ID, PRIMARY_REVISION, "BTF_MODEL_ID", "BTF_MODEL_REVISION")
SECONDARY = _Detector(SECONDARY_ID, SECONDARY_REVISION, "BTF_MODEL2_ID", "BTF_MODEL2_REVISION")


def model_id() -> str:
    """The primary model id (BTF_MODEL_ID)."""
    return PRIMARY.model_id()


def model_revision() -> str | None:
    """The primary model revision: pinned for the default model, BTF_MODEL_REVISION otherwise."""
    return PRIMARY.revision()


def enabled() -> bool:
    return os.environ.get("BTF_MODEL", "").strip().lower() not in _OFF


def ensemble() -> bool:
    """Agreement mode (both models). BTF_MODEL_ENSEMBLE=off runs the secondary model alone."""
    return os.environ.get("BTF_MODEL_ENSEMBLE", "").strip().lower() not in _OFF


def deps_installed() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers"))


def _active() -> list[_Detector]:
    return [PRIMARY, SECONDARY] if ensemble() else [SECONDARY]


def status() -> Status:
    """"loaded" once every configured model has loaded or failed and at least one works."""
    if not enabled():
        return "off"
    active = _active()
    if not deps_installed() or all(d.failed for d in active):
        return "unavailable"
    if all(d.loaded or d.failed for d in active):
        return "loaded"
    return "loading"  # warming up in the background, or will load on the first scan


def _load_all() -> None:
    for d in _active():
        d.load()


def warmup() -> None:
    """Load in a background thread so the first scan is not slow."""
    if enabled() and deps_installed() and any(not d.loaded and not d.failed for d in _active()):
        threading.Thread(target=_load_all, name="btf-model-warmup", daemon=True).start()


def name() -> str:
    """Display name of the configured detector setup."""
    if not ensemble():
        return SECONDARY.short_name()
    return f"{PRIMARY.model_id()} + {SECONDARY.short_name()} (agreement)"


def decide(h: float | None, c: float | None, agreement: bool = True) -> Tier:
    """The detector decision for one image. `h`/`c`: primary/secondary P(AI), None if that model did not run.

    In agreement mode a single model can never produce "likely". With agreement=False only `c` is used, with the
    secondary model's previous standalone thresholds.
    """
    if not agreement:
        if c is None:
            return "none"
        return "likely" if c >= C_LIKELY_ALONE else "possible" if c >= C_POSSIBLE else "none"
    if h is not None and c is not None:
        if h >= H_LIKELY and c >= C_AGREE:
            return "likely"
        return "possible" if h >= H_LIKELY or c >= C_POSSIBLE else "none"
    if h is not None:
        return "possible" if h >= H_LIKELY else "none"
    if c is not None:
        return "possible" if c >= C_POSSIBLE else "none"
    return "none"


def top_half(values: list[float]) -> list[float]:
    """The most suspicious half of the values (rounded up), highest first."""
    return sorted(values, reverse=True)[: (len(values) + 1) // 2]


def top_half_mean(values: list[float]) -> float:
    top = top_half(values)
    return sum(top) / len(top)


def aggregate(tiers: list[Tier]) -> Tier:
    """Video decision from per-frame decisions: the tier reached by a majority of the most suspicious half.

    A partly synthetic clip is not diluted by its real frames, while one odd frame cannot decide a clip of three
    or more frames.
    """
    ranked = sorted(tiers, key=TIER_RANK.__getitem__, reverse=True)
    top = ranked[: (len(ranked) + 1) // 2]
    return top[len(top) // 2]


@dataclass(frozen=True)
class Prediction:
    """Per-image scores from each model. A list is None when that model was not configured or failed to load."""

    h: list[float] | None
    c: list[float] | None
    agreement: bool = True
    h_name: str = PRIMARY_ID
    c_name: str = SECONDARY_ID.rsplit("/", 1)[-1]
    name: str = ""  # display name; empty -> derived from which models ran

    def __post_init__(self) -> None:
        if self.h is None and self.c is None:
            raise ValueError("a prediction needs at least one model's scores")
        if self.h is not None and self.c is not None and len(self.h) != len(self.c):
            raise ValueError("both models must score every image")

    def __len__(self) -> int:
        return len(self.h if self.h is not None else self.c)

    @property
    def degraded(self) -> bool:
        """Agreement mode, but only one of the two models ran."""
        return self.agreement and (self.h is None or self.c is None)

    @property
    def missing(self) -> str | None:
        """Name of the model that did not run in a degraded agreement-mode prediction."""
        if not self.degraded:
            return None
        return self.h_name if self.h is None else self.c_name

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.agreement and not self.degraded:
            return f"{self.h_name} + {self.c_name} (agreement)"
        return self.h_name if self.h is not None else self.c_name

    def models(self) -> list[tuple[str, list[float]]]:
        """(name, per-image probabilities) for each model that ran, primary first."""
        out = []
        if self.h is not None:
            out.append((self.h_name, self.h))
        if self.c is not None:
            out.append((self.c_name, self.c))
        return out

    def tiers(self) -> list[Tier]:
        n = len(self)
        return [
            decide(self.h[i] if self.h is not None else None, self.c[i] if self.c is not None else None, self.agreement)
            for i in range(n)
        ]


def unavailable_models() -> list[str]:
    """Names of configured models that failed to load."""
    return [d.short_name() for d in _active() if d.failed]


def predict(images: list[Image.Image]) -> Prediction | None:
    """Score images with every configured model, or None if the detector is off or no model could run."""
    if not images or not enabled() or not deps_installed():
        return None
    if not ensemble():
        c = SECONDARY.predict(images)
        if c is None:
            return None
        return Prediction(h=None, c=c, agreement=False, c_name=SECONDARY.short_name())
    h = PRIMARY.predict(images)
    c = SECONDARY.predict(images)
    if h is None and c is None:
        return None
    pred = Prediction(h=h, c=c, h_name=PRIMARY.model_id(), c_name=SECONDARY.short_name())
    if pred.degraded:
        log.warning("detector running degraded: %s unavailable", ", ".join(unavailable_models()))
    return pred
