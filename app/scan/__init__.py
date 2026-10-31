"""Media scanning. Public interface: `scan_file`, `capabilities`, `warmup` and `UnsupportedMedia`.

Pipeline: raw-byte markers -> image metadata (EXIF/XMP/text chunks) or video container metadata ->
C2PA content credentials -> optional ML detector -> verdict.
"""

import logging
import math
import os
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from app.scan import c2pa_check, exif, markers, model, video
from app.scan.markers import Hit

log = logging.getLogger("banthefake.scan")

Verdict = Literal["verified_capture", "verified_ai", "ai", "edited", "camera", "unknown"]
SignalKind = Literal["ai", "edit", "camera", "c2pa", "model", "info"]

NO_PROOF = "No tool can prove a file is real."


class UnsupportedMedia(Exception):
    """The file passed sniffing but cannot be scanned safely (e.g. decompression bomb)."""


class Signal(TypedDict):
    kind: SignalKind
    text: str
    weight: float
    field: NotRequired[str]  # marker signals: the metadata field the marker was found in
    snippet: NotRequired[str]  # marker signals: the matched text (at most 80 characters)


class C2PAInfo(TypedDict):
    present: bool
    valid: bool
    trusted: bool
    generator: str
    issuer: str
    signer: str
    signed_at: str
    capture: bool  # declared camera capture from a valid, trusted manifest only
    claimed_capture: bool  # declared camera capture, verified or not
    ai_generated: bool  # the manifest or one of its ingredients' manifests declares generative AI
    ingredient_ai: bool  # an ingredient's own manifest declares generative AI
    actions: list[str]
    edits: list[str]
    ingredients: list[dict[str, str]]
    trust_list_version: str


class Coverage(TypedDict):
    first_s: float  # timestamp of the first sampled frame
    last_s: float  # timestamp of the last sampled frame
    duration_s: float | None  # video duration, if known


class ModelScore(TypedDict):
    name: str
    ai_probability: float  # this model's P(AI); for videos, the mean of its most suspicious half of frames
    frame_probabilities: NotRequired[list[float]]


class ModelInfo(TypedDict):
    name: str
    # The primary model's P(AI) (haywoodsloan), kept for compatibility. If only one model ran (the other failed to
    # load, or BTF_MODEL_ENSEMBLE=off), it is that model's probability. The verdict comes from `decision`.
    ai_probability: float
    decision: model.Tier  # combined decision: "likely" | "possible" | "none"
    models: list[ModelScore]  # every model that ran, primary first
    frames_checked: int
    low_colour: NotRequired[bool]  # set (True) when the scores are outside the detector's reliable range
    frames_likely: NotRequired[int]  # videos: sampled frames individually rated "likely"
    frames_possible: NotRequired[int]  # videos: sampled frames individually rated "possible"
    frame_probabilities: NotRequired[list[float]]
    frame_timestamps: NotRequired[list[float]]
    coverage: NotRequired[Coverage]


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
    model: Literal["loaded", "degraded", "loading", "off", "unavailable"]
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
                sig = _sig(h.kind, h.text, h.weight)
                if h.field:
                    sig["field"] = h.field
                    sig["snippet"] = h.snippet
                self.signals.append(sig)

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
    tool_fields: list[tuple[str, str]] = []
    if cred is not None and cred.readable and cred.valid and not cred.ai_generated:
        # an AI declaration is reported once, by the C2PA signal; otherwise check the named tools
        tool_fields.append(("C2PA claim generator", cred.generator))
        tool_fields += [("C2PA softwareAgent", a) for a in cred.software_agents]
    chunk_hits: list[Hit] = []
    low_colour = False

    # --- format-specific metadata + pixels for the model ---------------------------------------
    frames = []
    timestamps: list[float] = []
    duration: float | None = None
    camera = ""
    camera_details = False
    detector_reason = "the file could not be decoded"  # why the model could not run, if it did not
    if kind == "image":
        try:
            meta = exif.extract(path)
        except exif.ImageTooLarge as exc:
            raise UnsupportedMedia("Image dimensions are too large to scan.") from exc
        if meta.error:
            out.add("info", "The image could not be decoded; only its raw bytes were checked.", 0.1)
        else:
            file_info["width"], file_info["height"] = meta.width, meta.height
        tool_fields += meta.tool_fields()
        # generator settings stored in (possibly compressed) text chunks
        for key, value in meta.text_chunks.items():
            chunk_hits += markers.scan_generator_metadata(
                value.encode("utf-8", "replace"), meta.text_field(key), key
            )
        if meta.user_comment:
            chunk_hits += markers.scan_generator_metadata(
                meta.user_comment.encode("utf-8", "replace"), "EXIF UserComment", "usercomment"
            )
        camera = meta.camera if (meta.make or meta.model) else ""
        camera_details = meta.camera_details
        has_metadata = meta.has_exif or bool(meta.xmp) or bool(meta.text_chunks)
        if meta.error:
            detector_reason = "the image could not be opened"
        elif model.enabled():
            try:
                decoded = exif.decode_for_model(path)
                frames = [decoded.image]
                low_colour = decoded.low_colour
                for note in decoded.notes:
                    out.add("info", f"Note: {note}.", 0.1)
            except exif.ImageTooLarge as exc:
                raise UnsupportedMedia("Image dimensions are too large to scan.") from exc
            except exif.DecodeFailed as exc:
                detector_reason = exc.reason
            except Exception as exc:
                log.info("image decode for model failed: %s", type(exc).__name__)
                detector_reason = f"the image could not be decoded ({type(exc).__name__})"
    else:
        vid = video.probe(path, sample=video.SAMPLE_FRAMES if model.enabled() else 0)
        if vid.audio_only:
            raise UnsupportedMedia("This file has no video track (audio-only files cannot be scanned). "
                                   "Upload an image or a video.")
        if vid.width and vid.height:
            file_info["width"], file_info["height"] = vid.width, vid.height
        if vid.duration_s:
            file_info["duration_s"] = round(vid.duration_s, 2)
        if vid.error:
            out.add("info", f"Video check limited: {vid.error}.", 0.1)
        tool_fields += vid.tool_fields()
        camera = " ".join(x for x in (vid.make, vid.model) if x)
        frames = vid.frames
        timestamps, duration = vid.timestamps, vid.duration_s
        detector_reason = vid.error or "no video frames could be decoded"
        has_metadata = bool(camera) or any(k.startswith("com.apple") for k in vid.metadata)

    text_hits = markers.scan_text(tool_fields)

    # --- assemble signals in priority order ----------------------------------------------------
    if cred is not None:
        _c2pa_signals(out, cred)
    # most specific source first: a named metadata field beats a match somewhere in the raw bytes
    metadata_hits = text_hits + chunk_hits
    if cred is None:
        metadata_hits += raw_hits
    out.hits([h for h in metadata_hits if h.kind == "ai"])
    out.hits([h for h in text_hits if h.kind == "edit"])
    if cred is not None and cred.readable and cred.valid and cred.trusted and cred.edit_actions:
        names = ", ".join(_action_name(a) for a in cred.edit_actions[:6])
        out.add("edit", f"Content credentials record edits: {names}.", 0.6)

    if camera:
        detail = " with exposure settings" if camera_details else ""
        out.add("camera", f"Camera data found ({camera}){detail}. Real cameras write this, but it can be copied or faked.", 0.4)

    if low_colour:
        out.add("info", "The AI detector is unreliable on black-and-white or low-colour images.", 0.1)
    model_info, model_failure = _run_model(out, frames, kind, timestamps, duration, low_colour)
    if cred is not None and cred.valid and cred.trusted and model_info is not None:
        decision = model_info["decision"]
        if cred.capture and decision == "likely":
            out.add("info", "The AI detector strongly disagrees with the trusted capture credentials; both results are shown, and the cryptographic provenance determines the verdict.", 0.5)
        elif cred.ai_generated and decision == "none":
            out.add("info", "The AI detector found no strong AI signal, but the trusted content credentials declare generative AI; both results are shown, and the cryptographic provenance determines the verdict.", 0.5)
    detector_failed = model.enabled() and model_info is None
    if detector_failed:
        # Never skip the model silently: an unanalyzed file must not look like a clean one.
        reason = model_failure or detector_reason
        out.add("info", f"AI detector could not analyze this file ({reason}).", 0.2)

    if not out.signals and kind == "image" and not has_metadata and not detector_failed:
        out.add("info", "No metadata at all. Often a sign it was stripped by a social app, or on purpose.", 0.1)

    verdict, title, detail = _verdict(out, cred, model_info, detector_failed, low_colour)
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
    if not cred.valid:
        problems = ", ".join(cred.failures[:3]) or "validation failed"
        out.add("info", f"Content credentials present but failed validation ({problems}) — ignored.", 0.2)
        return
    by = f" by “{cred.generator}”" if cred.generator else ""
    if cred.valid and cred.trusted:
        sig = f"signed by {cred.issuer or 'a trusted signer'}, and the signature checks out"
    elif cred.valid:
        sig = (
            f"signature is intact, but the signer ({cred.issuer or 'unknown'}) is not on our trust list, "
            "so who made it cannot be confirmed"
        )
    parts = f" The credentials include {_ingredients_text(cred)}." if cred.ingredients else ""
    out.add("c2pa", f"Content credentials (C2PA) found, made{by}; {sig}.{parts}", 0.7)
    if cred.ingredient_ai and not any(c2pa_check._is_ai(t) for t in cred.source_types):
        if cred.trusted:
            out.hits([Hit("ai", "c2pa_ingredient_ai", "Content credentials include an ingredient whose own content "
                          "credentials declare generative AI.", 1.0)])
        else:
            out.add("c2pa", "One of the ingredients has its own content credentials declaring generative AI. "
                            "Untrusted credentials are informational only.", 0.3)
    elif cred.ai_generated and cred.trusted:
        trained = any(t.rsplit("/", 1)[-1] == "trainedAlgorithmicMedia" for t in cred.source_types)
        key = "iptc_trained" if trained else "iptc_composite"
        source = next(t for t in cred.source_types if t.rsplit("/", 1)[-1] in c2pa_check.AI_SOURCE_TYPES)
        snippet = source.rsplit("/", 1)[-1][: markers.SNIPPET_MAX]
        out.hits([Hit("ai", key, "Content credentials declare this was made or changed with generative AI. "
                      f"Found in C2PA actions digitalSourceType: \u201c{snippet}\u201d", 1.0,
                      "C2PA actions digitalSourceType", snippet)])
    if cred.source_types and not (cred.trusted and any(c2pa_check._is_ai(t) for t in cred.source_types)):
        kinds = ", ".join(dict.fromkeys(t.rsplit("/", 1)[-1] for t in cred.source_types))
        qualifier = "" if cred.trusted else " Untrusted credentials are informational only."
        generated = "" if cred.ai_generated else " (not generative AI)"
        out.add("c2pa", f"Content credentials list the source type as: {kinds}{generated}.{qualifier}", 0.3)


def _run_model(
    out: _Collector, frames: list, kind: str, timestamps: list[float] | None = None,
    duration: float | None = None, low_colour: bool = False,
) -> tuple[ModelInfo | None, str | None]:
    """Run the detectors. Returns (info, None), or (None, reason) when they ran but gave no result."""
    if not frames:
        return None, None
    try:
        pred = model.predict(frames)
    except Exception:
        log.exception("model inference failed")
        return None, "the detector failed on this file"
    if pred is None:
        return None, "the detector is not available on this server"
    if not all(math.isfinite(x) for _, probs in pred.models() for x in probs):
        log.warning("detector returned a non-finite score")
        return None, "the detector returned an invalid score"
    tiers = pred.tiers()
    video_mode = kind == "video" and len(tiers) > 1
    # Video: decide per frame, then take the tier a majority of the most suspicious half of frames reaches. Any
    # frame rated "likely" on its own makes the clip at least "possible": a partly synthetic clip is never reported
    # as clean. Reported probabilities are the mean of each model's most suspicious half, as before.
    decision = model.aggregate(tiers) if video_mode else tiers[0]
    over = sum(t == "likely" for t in tiers)
    some = sum(t == "possible" for t in tiers)
    if video_mode and over and decision == "none":
        decision = "possible"
    scores: list[ModelScore] = []
    for name, probs in pred.models():
        entry: ModelScore = {
            "name": name, "ai_probability": round(model.top_half_mean(probs) if video_mode else probs[0], 4),
        }
        if kind == "video":
            entry["frame_probabilities"] = [round(x, 4) for x in probs]
        scores.append(entry)
    primary = pred.models()[0][1]
    p = scores[0]["ai_probability"]
    info: ModelInfo = {
        "name": pred.display_name, "ai_probability": p, "decision": decision, "models": scores,
        "frames_checked": len(tiers),
    }
    if low_colour:
        info["low_colour"] = True
    coverage_note = partial = ""
    if kind == "video":
        info["frames_likely"], info["frames_possible"] = over, some
        info["frame_probabilities"] = [round(x, 4) for x in primary]
        if timestamps and len(timestamps) == len(tiers):
            first, last = timestamps[0], timestamps[-1]
            dur = round(duration, 2) if duration else None
            info["frame_timestamps"] = timestamps
            info["coverage"] = {"first_s": first, "last_s": last, "duration_s": dur}
            coverage_note = f", taken from {first:.1f} s to {last:.1f} s" + (f" of {dur:.1f} s" if dur else "")
            if dur and dur > 2 and last < 0.75 * dur:
                partial = (f"The detector only reached {last:.1f} s of this {dur:.1f} s video (time limit), "
                           "so the rest was not checked.")
    # when only some frames are "likely", the "possible" wording below states the count itself
    count_note = "" if decision == "possible" and over else f"; {over} of {len(tiers)} rated likely AI"
    frames_note = (
        f" (based on the most suspicious half of {len(tiers)} sampled frames{coverage_note}{count_note})"
        if video_mode else ""
    )
    weight = max(s["ai_probability"] for s in scores)
    score_text = _scores_text(scores)
    single = not pred.agreement  # a single configured model (BTF_MODEL_ENSEMBLE=off)
    our = "Our detector model" if single else "Our detector models"
    if pred.missing:
        out.add("info", f"One of the two detector models ({pred.missing}) could not be loaded, so this result comes from "
                        "one model only and can at most show possible signs of AI.", 0.2)
    if low_colour:
        # Outside the reliable range the scores are shown for reference only, in neutral wording for every tier.
        out.add("model", f"{our} scored this image {score_text}, but black-and-white and low-colour images are outside "
                         "their reliable range, so these scores do not point either way.", 0.1)
    elif decision == "likely":
        if single:
            out.add("model", f"Our detector model rates this {score_text} likely to be AI-generated{frames_note}. Detectors can be wrong.", weight)
        else:
            out.add("model", f"Both detector models agree this looks AI-generated ({score_text}){frames_note}. Detectors can be wrong.", weight)
    elif decision == "possible":
        if video_mode and over:
            why = (f"only {over} of {len(tiers)} sampled frames were individually rated likely AI-generated, too few "
                   "to call the whole clip likely")
        elif pred.missing:
            why = "only one detector model was available"
        elif single:
            why = ""
        else:
            why = "the two detector models do not both agree"
        sees = "sees" if single else "see"
        tail = f", but {why}" if why else ""
        out.add("model", f"{our} {sees} possible signs of AI generation ({score_text}){frames_note}{tail}. Not conclusive.", weight)
    elif video_mode and some:
        out.add("model", f"{our} found no consistent AI signal across the clip ({score_text}){frames_note}, but {some} of "
                         f"{len(tiers)} sampled frames individually showed possible signs. Not conclusive, and this does "
                         "not mean it is real.", weight)
    else:
        out.add("model", f"{our} found no strong AI signal ({score_text}){frames_note}. Detectors miss many fakes, so this does not mean it is real.", weight)
    if partial:
        out.add("info", partial, 0.2)
    if kind == "video" and duration is None:
        out.add("info", "The video duration is unknown, so detector coverage may be partial.", 0.2)
    return info, None


def _ingredients_text(cred) -> str:
    n = len(cred.ingredients)
    return f"{n} ingredient{'' if n == 1 else 's'} (other files placed or combined into this one)"


def _scores_text(scores: list[ModelScore]) -> str:
    """"ai-image-detector-deploy 99%, CommunityForensics-DeepfakeDet-ViT 97%", or "97%" for a single model."""
    if len(scores) == 1:
        return f"{_pct(scores[0]['ai_probability'])}%"
    return ", ".join(f"{s['name'].rsplit('/', 1)[-1]} {_pct(s['ai_probability'])}%" for s in scores)


def _pct(p: float) -> int:
    """Whole percent, never shown as a certain 0% or 100%."""
    return max(1, min(99, round(p * 100)))


def _verdict(
    out: _Collector, cred, model_info: ModelInfo | None, detector_failed: bool = False, low_colour: bool = False
) -> tuple[Verdict, str, str]:
    decision = model_info["decision"] if model_info else None
    scores = _scores_text(model_info["models"]) if model_info else "n/a"
    parts = f" The credentials include {_ingredients_text(cred)}." if cred is not None and cred.ingredients else ""
    if cred is not None and cred.readable and cred.valid and cred.trusted and cred.capture:
        signer = cred.signer or cred.issuer or "a trusted signer"
        if cred.edit_actions:
            edits = ", ".join(_action_name(a) for a in cred.edit_actions[:6])
            return (
                "verified_capture", "Verified capture with recorded edits",
                f"Trusted credentials signed by {signer} declare a camera capture; recorded edits: {edits}.{parts} "
                "The file is unchanged since the latest signed edit. This confirms the signed provenance, not that the scene "
                "itself is real (for example, it could be a photo of a screen).",
            )
        return (
            "verified_capture", "Verified camera capture",
            f"Trusted credentials signed by {signer} declare a camera capture, and the file is unchanged since signing. "
            "This confirms the signed provenance, not that the scene itself is real (for example, it could be a photo of a screen).",
        )
    if cred is not None and cred.readable and cred.valid and cred.trusted and cred.ai_generated:
        signer = cred.signer or cred.issuer or "a trusted signer"
        if not any(c2pa_check._is_ai(t) for t in cred.source_types):  # declared only by an ingredient
            return (
                "verified_ai", "Verified AI provenance",
                f"Trusted content credentials signed by {signer} include {_ingredients_text(cred)}, and an ingredient's "
                "own content credentials declare generative AI. The file is unchanged since signing.",
            )
        return (
            "verified_ai", "Verified AI provenance",
            f"Trusted content credentials signed by {signer} declare generative AI, and the file is unchanged since signing.{parts}",
        )
    if out.has("ai"):
        declared = cred is not None and cred.readable and cred.valid and cred.ai_generated
        detail = (
            "The file's content credentials declare AI involvement." if declared
            else "The file carries markers left by AI generation tools."
        )
        return "ai", "AI-generation markers found", f"{detail} Markers can be removed, so their absence elsewhere proves nothing. {NO_PROOF}"
    if decision == "likely" and not low_colour:  # never "likely" on black-and-white or low-colour images
        agree = "both of our detector models agree it looks" if len(model_info["models"]) > 1 else "our detector model rates it"
        return (
            "ai",
            "Likely AI-generated",
            f"No AI metadata, but {agree} AI-made ({scores}). "
            f"Detectors make mistakes, so check where the file came from. {NO_PROOF}",
        )
    if detector_failed:
        return "unknown", "Could not fully analyze", f"The AI detector could not analyze this file. {NO_PROOF}"
    if out.has("edit"):
        return (
            "edited",
            "Edited, no AI markers found",
            f"It went through editing software. Edits can be harmless, so check the source. {NO_PROOF}",
        )
    if low_colour:
        return (
            "unknown", "Detector unreliable for this image",
            f"Black-and-white and low-colour images are outside the detector's reliable range, so its scores "
            f"({scores}) cannot give an AI verdict either way. {NO_PROOF}",
        )
    if decision == "possible":
        likely_frames = model_info.get("frames_likely", 0) if model_info else 0
        frames = (
            f" {likely_frames} of {model_info['frames_checked']} sampled frames were individually rated likely "
            "AI-generated, so part of the clip may be synthetic."
            if likely_frames and model_info["frames_checked"] > 1 else ""
        )
        return (
            "unknown",
            "Possible signs of AI generation",
            f"Our detector model sees some signs of AI ({scores}), but not enough to say so.{frames} {NO_PROOF}",
        )
    if out.has("camera"):
        return (
            "camera",
            "Camera data found, no AI markers",
            f"The file has camera metadata. That is a good sign, but it can be faked or copied, and it does NOT mean the file is real. {NO_PROOF}",
        )
    return "unknown", "No conclusive signals found", f"That does NOT mean it is real. Many fakes have their metadata removed. {NO_PROOF}"
