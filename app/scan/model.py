"""Optional AI-image detector (ViT, single logit, sigmoid = P(AI-generated)).

Needs the `ml` extra (torch + transformers). Lazy-loaded once, thread-safe. Disabled with BTF_MODEL=off.
Model id and revision can be overridden with BTF_MODEL_ID and BTF_MODEL_REVISION; weights are fetched from
the Hugging Face Hub on first use (set HF_HOME to control the cache, HF_HUB_OFFLINE=1 to forbid downloads).
"""

import importlib.util
import logging
import os
import threading
from typing import Literal

from PIL import Image

log = logging.getLogger("banthefake.model")

DEFAULT_MODEL_ID = "buildborderless/CommunityForensics-DeepfakeDet-ViT"
DEFAULT_MODEL_REVISION = "ac6ee457bea904a373065754107451793b56db00"
LIKELY = 0.85  # >= : "likely AI"
POSSIBLE = 0.5  # >= : "possible signs"
BATCH = 8

Status = Literal["loaded", "loading", "off", "unavailable"]

_load_lock = threading.Lock()
_infer_lock = threading.Lock()
_model = None
_processor = None
_failed = False
_loading = False


def model_id() -> str:
    return os.environ.get("BTF_MODEL_ID", "").strip() or DEFAULT_MODEL_ID


def model_revision() -> str | None:
    override = os.environ.get("BTF_MODEL_REVISION", "").strip()
    if override:
        return override
    return DEFAULT_MODEL_REVISION if model_id() == DEFAULT_MODEL_ID else None


def enabled() -> bool:
    return os.environ.get("BTF_MODEL", "").strip().lower() not in ("off", "0", "false", "no")


def deps_installed() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers"))


def status() -> Status:
    if not enabled():
        return "off"
    if _model is not None:
        return "loaded"
    if _failed or not deps_installed():
        return "unavailable"
    return "loading"  # warming up in the background, or will load on the first scan


def _load() -> bool:
    """Load the model once. Returns True when ready."""
    global _model, _processor, _failed, _loading
    if _model is not None:
        return True
    if _failed or not deps_installed():
        return False
    with _load_lock:
        if _model is not None:
            return True
        if _failed:
            return False
        _loading = True
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModelForImageClassification

            threads = os.environ.get("BTF_MODEL_THREADS")
            if threads and threads.isdigit():
                torch.set_num_threads(int(threads))
            mid = model_id()
            revision = model_revision()
            options = {"revision": revision} if revision else {}
            _processor = AutoImageProcessor.from_pretrained(mid, **options)
            _model = AutoModelForImageClassification.from_pretrained(
                mid, use_safetensors=True, **options
            ).eval()
            log.info("loaded model %s at revision %s", mid, revision or "default")
            return True
        except Exception:
            log.exception("could not load detector model")
            _failed = True
            return False
        finally:
            _loading = False


def warmup() -> None:
    """Load in a background thread so the first scan is not slow."""
    if enabled() and deps_installed() and _model is None and not _failed:
        threading.Thread(target=_load, name="btf-model-warmup", daemon=True).start()


def predict(images: list[Image.Image]) -> list[float] | None:
    """Return P(AI-generated) per image, or None if the model is off/unavailable."""
    if not images or not enabled() or not _load():
        return None
    import torch

    out: list[float] = []
    with _infer_lock, torch.inference_mode():
        for i in range(0, len(images), BATCH):
            batch = [im.convert("RGB") for im in images[i : i + BATCH]]
            inputs = _processor(batch, return_tensors="pt")
            logits = _model(**inputs).logits
            if logits.shape[-1] == 1:
                probs = torch.sigmoid(logits[:, 0])
            else:  # two-class head: assume the label containing "fake"/"ai" is the positive class
                idx = _fake_index()
                probs = logits.softmax(-1)[:, idx]
            out.extend(float(p) for p in probs)
    return out


def _fake_index() -> int:
    labels = {int(k): str(v).lower() for k, v in _model.config.id2label.items()}
    for i, name in labels.items():
        if any(w in name for w in ("fake", "ai", "artificial", "synthetic", "generated")):
            return i
    return 1


def name() -> str:
    return model_id().rsplit("/", 1)[-1]
