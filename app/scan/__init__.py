"""Media scanning. Public interface: `scan_file`, `capabilities`, `warmup` and `UnsupportedMedia`.

Pipeline: raw-byte markers -> image metadata (EXIF/XMP/text chunks) or video container metadata ->
C2PA content credentials -> optional ML detector -> verdict.
"""

import logging
import os
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from app.scan import c2pa_check, exif, markers, model, video
from app.scan.markers import Hit

log = logging.getLogger("banthefake.scan")

Verdict = Literal["ai", "edited", "camera", "unknown"]
SignalKind = Literal["ai", "edit", "camera", "c2pa", "model", "info"]

NO_PROOF = "No tool can prove a file is real."


class UnsupportedMedia(Exception):
    """The file passed sniffing but cannot be scanned safely (e.g. decompression bomb)."""


class Signal(TypedDict):
    kind: SignalKind
    text: str
    weight: float


class C2PAInfo(TypedDict):
    present: bool
    valid: bool
    trusted: bool
    generator: str
    issuer: str
    ai_generated: bool
    actions: list[str]


class ModelInfo(TypedDict):
    name: str
    ai_probability: float
    frames_checked: int
    frame_probabilities: NotRequired[list[float]]


class FileInfo(TypedDict):
    type: Literal["image", "video"]
    mime: str
    size_bytes: int
    width: NotRequired[int]
    height: NotRequired[int]
    duration_s: NotRequired[float]


class ScanReport(TypedDict):
    verdict: Verdict
    verdict_title: str
    verdict_detail: str
    signals: list[Signal]
    c2pa: C2PAInfo | None
    model: ModelInfo | None
    file: FileInfo


class Capabilities(TypedDict):
    model: Literal["loaded", "loading", "off", "unavailable"]
    c2pa: bool


def capabilities() -> Capabilities:
    return {"model": model.status(), "c2pa": c2pa_check.AVAILABLE}


def warmup() -> None:
    model.warmup()


def _sig(kind: SignalKind, text: str, weight: float) -> Signal:
    return {"kind": kind, "text": text, "weight": round(max(0.0, min(1.0, weight)), 3)}


class _Collector:
    def __init__(self) -> None:
        self.signals: list[Signal] = []
        self._keys: set[str] = set()

    def hits(self, hits: list[Hit]) -> None:
        for h in hits:
            if h.key not in self._keys:
                self._keys.add(h.key)
                self.signals.append(_sig(h.kind, h.text, h.weight))

    def add(self, kind: SignalKind, text: str, weight: float) -> None:
        self.signals.append(_sig(kind, text, weight))

    def has(self, kind: SignalKind) -> bool:
        return any(s["kind"] == kind for s in self.signals)


def scan_file(path: str | Path, filename: str, content_type: str) -> ScanReport:
    """Scan the media file at `path`. `content_type` is the server-sniffed MIME type."""
    kind: Literal["image", "video"] = "video" if content_type.startswith("video/") else "image"
    file_info: FileInfo = {"type": kind, "mime": content_type, "size_bytes": os.path.getsize(path)}
    out = _Collector()

    raw = markers.read_window(path)
    raw_hits = markers.scan_bytes(raw)

    # --- C2PA ---------------------------------------------------------------------------------
    cred = c2pa_check.read(path, content_type)
    if cred is not None and not cred.readable:
        cred = c2pa_check.C2PAResult(present=True, readable=False) if markers.has_c2pa_hint(raw) else None
    tool_strings: list[str] = []
    if cred is not None and cred.readable and not cred.ai_generated:
        # an AI declaration is reported once, by the C2PA signal; otherwise check the named tools
        tool_strings += [cred.generator, *cred.software_agents]

    # --- format-specific metadata + pixels for the model ---------------------------------------
    frames = []
    camera = ""
    camera_details = False
    if kind == "image":
        try:
            meta = exif.extract(path)
        except exif.ImageTooLarge as exc:
            raise UnsupportedMedia("Image dimensions are too large to scan.") from exc
        if meta.error:
            out.add("info", "The image could not be decoded; only its raw bytes were checked.", 0.1)
        else:
            file_info["width"], file_info["height"] = meta.width, meta.height
        tool_strings += meta.tool_strings()
        # generator settings stored in (possibly compressed) text chunks
        raw_hits += markers.scan_bytes("\n".join(meta.text_chunks.values()).encode("utf-8", "replace"))
        camera = meta.camera if (meta.make or meta.model) else ""
        camera_details = meta.camera_details
        has_metadata = meta.has_exif or bool(meta.xmp) or bool(meta.text_chunks)
        if not meta.error and model.enabled():
            try:
                img = exif.load_for_model(path)
                if img is not None:
                    frames = [img]
            except exif.ImageTooLarge as exc:
                raise UnsupportedMedia("Image dimensions are too large to scan.") from exc
            except Exception as exc:
                log.info("image decode for model failed: %s", type(exc).__name__)
    else:
        vid = video.probe(path, sample=video.SAMPLE_FRAMES if model.enabled() else 0)
        if vid.width and vid.height:
            file_info["width"], file_info["height"] = vid.width, vid.height
        if vid.duration_s:
            file_info["duration_s"] = round(vid.duration_s, 2)
        if vid.error:
            out.add("info", f"Video check limited: {vid.error}.", 0.1)
        tool_strings += vid.tool_strings()
        camera = " ".join(x for x in (vid.make, vid.model) if x)
        frames = vid.frames
        has_metadata = bool(camera) or any(k.startswith("com.apple") for k in vid.metadata)

    text_hits = markers.scan_text([s for s in tool_strings if s])

    # --- assemble signals in priority order ----------------------------------------------------
    if cred is not None:
        _c2pa_signals(out, cred)
    out.hits([h for h in raw_hits + text_hits if h.kind == "ai"])
    out.hits([h for h in text_hits if h.kind == "edit"])
    if cred is not None and cred.readable and cred.edit_actions:
        names = ", ".join(_action_name(a) for a in cred.edit_actions[:6])
        out.add("edit", f"Content credentials record edits: {names}.", 0.6)

    if camera:
        detail = " with exposure settings" if camera_details else ""
        out.add("camera", f"Camera data found ({camera}){detail}. Real cameras write this, but it can be copied or faked.", 0.4)

    model_info = _run_model(out, frames, kind)

    if not out.signals and kind == "image" and not has_metadata:
        out.add("info", "No metadata at all. Often a sign it was stripped by a social app, or on purpose.", 0.1)

    verdict, title, detail = _verdict(out, cred, model_info)
    return {
        "verdict": verdict,
        "verdict_title": title,
        "verdict_detail": detail,
        "signals": out.signals,
        "c2pa": cred.to_report() if cred is not None else None,  # type: ignore[typeddict-item]
        "model": model_info,
        "file": file_info,
    }


def _action_name(action: str) -> str:
    return action.removeprefix("c2pa.").replace("_", " ")


def _c2pa_signals(out: _Collector, cred: c2pa_check.C2PAResult) -> None:
    if not cred.readable:
        out.add("c2pa", "The file seems to contain content credentials (C2PA), but they could not be read.", 0.3)
        return
    by = f" by “{cred.generator}”" if cred.generator else ""
    if cred.valid and cred.trusted:
        sig = f"signed by {cred.issuer or 'a trusted signer'}, and the signature checks out"
    elif cred.valid:
        sig = (
            f"signature is intact, but the signer ({cred.issuer or 'unknown'}) is not on our trust list, "
            "so who made it cannot be confirmed"
        )
    else:
        problems = ", ".join(cred.failures[:3]) or "validation failed"
        sig = f"but they do NOT validate ({problems}); the file may have been changed after signing"
    out.add("c2pa", f"Content credentials (C2PA) found, made{by}; {sig}.", 0.7 if cred.valid else 0.5)
    if cred.ai_generated:
        trained = any(t.rsplit("/", 1)[-1] == "trainedAlgorithmicMedia" for t in cred.source_types)
        key = "iptc_trained" if trained else "iptc_composite"
        out.hits([Hit("ai", key, "Content credentials declare this was made or changed with generative AI.", 1.0)])
    elif cred.source_types:
        kinds = ", ".join(dict.fromkeys(t.rsplit("/", 1)[-1] for t in cred.source_types))
        out.add("c2pa", f"Content credentials list the source type as: {kinds} (not generative AI).", 0.3)


def _run_model(out: _Collector, frames: list, kind: str) -> ModelInfo | None:
    if not frames:
        return None
    try:
        probs = model.predict(frames)
    except Exception:
        log.exception("model inference failed")
        return None
    if not probs:
        return None
    if kind == "video" and len(probs) > 1:
        # Average of the most suspicious half of the frames: a partly synthetic clip is not diluted
        # by its real frames, while a single odd frame cannot dominate.
        top = sorted(probs, reverse=True)[: (len(probs) + 1) // 2]
        p = sum(top) / len(top)
    else:
        p = probs[0]
    info: ModelInfo = {"name": model.name(), "ai_probability": round(p, 4), "frames_checked": len(probs)}
    if kind == "video":
        info["frame_probabilities"] = [round(x, 4) for x in probs]
    pct = _pct(p)
    over = sum(x >= model.LIKELY for x in probs)
    frames_note = (
        f" (based on the most suspicious half of {len(probs)} sampled frames; {over} of {len(probs)} rated likely AI)"
        if kind == "video" and len(probs) > 1 else ""
    )
    if p >= model.LIKELY:
        out.add("model", f"Our detector model rates this {pct}% likely to be AI-generated{frames_note}. Detectors can be wrong.", p)
    elif p >= model.POSSIBLE:
        out.add("model", f"Our detector model sees possible signs of AI generation ({pct}%){frames_note}. Not conclusive.", p)
    else:
        out.add("model", f"Our detector model found no strong AI signal ({pct}%){frames_note}. Detectors miss many fakes, so this does not mean it is real.", p)
    return info


def _pct(p: float) -> int:
    """Whole percent, never shown as a certain 0% or 100%."""
    return max(1, min(99, round(p * 100)))


def _verdict(out: _Collector, cred, model_info: ModelInfo | None) -> tuple[Verdict, str, str]:
    prob = model_info["ai_probability"] if model_info else None
    if out.has("ai"):
        declared = cred is not None and cred.readable and cred.ai_generated
        detail = (
            "The file's content credentials declare AI involvement." if declared
            else "The file carries markers left by AI generation tools."
        )
        return "ai", "AI-generation markers found", f"{detail} Markers can be removed, so their absence elsewhere proves nothing. {NO_PROOF}"
    if prob is not None and prob >= model.LIKELY:
        return (
            "ai",
            "Likely AI-generated",
            f"No AI metadata, but our detector model rates it {_pct(prob)}% likely AI-made. "
            f"Detectors make mistakes, so check where the file came from. {NO_PROOF}",
        )
    if out.has("edit"):
        return (
            "edited",
            "Edited, no AI markers found",
            f"It went through editing software. Edits can be harmless, so check the source. {NO_PROOF}",
        )
    if prob is not None and prob >= model.POSSIBLE:
        return (
            "unknown",
            "Possible signs of AI generation",
            f"Our detector model sees some signs of AI ({_pct(prob)}%), but not enough to say so. {NO_PROOF}",
        )
    if out.has("camera"):
        return (
            "camera",
            "Camera data found, no AI markers",
            f"The file has camera metadata. That is a good sign, but it can be faked or copied, and it does NOT mean the file is real. {NO_PROOF}",
        )
    return "unknown", "No conclusive signals found", f"That does NOT mean it is real. Many fakes have their metadata removed. {NO_PROOF}"
