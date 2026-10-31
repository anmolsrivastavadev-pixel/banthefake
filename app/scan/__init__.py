"""Media scanning. Public interface: `scan_file` and `capabilities`.

This is a stub; the real detectors (markers, EXIF, C2PA, model, video) plug in here.
"""

import os
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

Verdict = Literal["ai", "edited", "camera", "unknown"]
SignalKind = Literal["ai", "edit", "camera", "c2pa", "model", "info"]

NO_PROOF = "No tool can prove a file is real."


class Signal(TypedDict):
    kind: SignalKind
    text: str
    weight: float


class C2PAInfo(TypedDict):
    present: bool
    valid: bool
    generator: str
    issuer: str
    ai_generated: bool
    actions: list[str]


class ModelInfo(TypedDict):
    name: str
    ai_probability: float
    frames_checked: int


class FileInfo(TypedDict):
    type: Literal["image", "video"]
    mime: str
    size_bytes: int
    width: NotRequired[int]
    height: NotRequired[int]


class ScanReport(TypedDict):
    verdict: Verdict
    verdict_title: str
    verdict_detail: str
    signals: list[Signal]
    c2pa: C2PAInfo | None
    model: ModelInfo | None
    file: FileInfo


class Capabilities(TypedDict):
    model: Literal["loaded", "off", "unavailable"]
    c2pa: bool


def capabilities() -> Capabilities:
    model = "off" if os.environ.get("BTF_MODEL", "").strip().lower() == "off" else "unavailable"
    return {"model": model, "c2pa": False}


def scan_file(path: str | Path, filename: str, content_type: str) -> ScanReport:
    """Scan the media file at `path`. `content_type` is the server-sniffed MIME type."""
    kind: Literal["image", "video"] = "video" if content_type.startswith("video/") else "image"
    return {
        "verdict": "unknown",
        "verdict_title": "No conclusive signals found",
        "verdict_detail": f"That does NOT mean it is real. {NO_PROOF}",
        "signals": [],
        "c2pa": None,
        "model": None,
        "file": {"type": kind, "mime": content_type, "size_bytes": os.path.getsize(path)},
    }
