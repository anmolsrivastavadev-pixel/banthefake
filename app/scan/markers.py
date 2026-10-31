"""Marker scanning: known AI generators, editing apps and IPTC digital-source-type values.

Two tiers, to keep false positives down:

* RAW markers are structured values that only generation tools write: the IPTC digital-source-type
  URIs for generative AI, Stable Diffusion "Steps: N, Sampler:" settings and ComfyUI workflow JSON.
  They are searched in the raw bytes (first and last few MB only) and in decoded text chunks.
  Brand names are never searched in raw bytes: they also occur in free text (captions, titles,
  descriptions, copyright notices, comments), where they say nothing about how the file was made.
* TEXT markers are brand names ("Midjourney", "Firefly", "Sora", "Photoshop"...). They are only matched
  against metadata fields that name the creating software (EXIF Software/ProcessingSoftware, XMP
  CreatorTool/softwareAgent, PNG generator keys, C2PA claim generator, video encoder/handler tags).

Every hit records the field it was found in and a short snippet of the matched value.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HitKind = Literal["ai", "edit", "info"]

RAW_WINDOW = 4 * 1024 * 1024  # bytes read from the start and from the end of the file
SNIPPET_MAX = 80  # max characters of matched text quoted in a signal


@dataclass(frozen=True)
class Hit:
    kind: HitKind
    key: str  # stable id, used for de-duplication
    text: str
    weight: float
    field: str = ""  # where it was found, e.g. "EXIF Software"
    snippet: str = ""  # the matched value, at most SNIPPET_MAX characters


@dataclass(frozen=True)
class _Marker:
    key: str
    kind: HitKind
    pattern: re.Pattern
    text: str
    weight: float


def _raw(key: str, kind: HitKind, pattern: bytes, text: str, weight: float, flags: int = 0) -> _Marker:
    return _Marker(key, kind, re.compile(pattern, flags), text, weight)


def _txt(key: str, kind: HitKind, pattern: str, text: str, weight: float, flags: int = 0) -> _Marker:
    return _Marker(key, kind, re.compile(pattern, flags), text, weight)


# Raw file bytes are unstructured and may contain captions, keywords, or invalid C2PA boxes. Generator
# declarations are therefore matched only after the format parser identifies their metadata property.
RAW_MARKERS: list[_Marker] = []

# Stable Diffusion WebUI (A1111/Forge) settings: "<prompt>\nSteps: N, Sampler: X, ... CFG scale: N, ... Seed: N".
# Matched in three linear-time stages (see _a1111_match) instead of one regex with `.+`/`.*?` gaps,
# which backtracked catastrophically on long non-matching values. No pattern below has nested or
# overlapping quantifiers: every repeated class is disjoint from what follows it.
# `Sampler:(?:\s*\n)?[^,\n]+,` is the old `Sampler:\s*[^,\n]+,` without its quadratic split of a long
# whitespace run between \s* and [^,\n]+ (both match spaces): any leading whitespace up to the last
# newline goes to the optional group, the rest of the line to [^,\n]+.
A1111_HEAD_RE = re.compile(rb"\nSteps:\s*\d+,\s*Sampler:(?:\s*\n)?[^,\n]+,")
A1111_CFG_RE = re.compile(rb"CFG scale:\s*[\d.]+,")
A1111_SEED_RE = re.compile(rb"Seed:\s*\d")
# ComfyUI API/workflow JSON: a node's "class_type" near its "inputs" (either order).
COMFY_CLASS_RE = re.compile(rb'"class_type"\s*:\s*"[A-Za-z][^"]*"')
COMFY_INPUTS = b'"inputs"'
COMFY_CLASS_KEY_RE = re.compile(rb'"class_type"\s*:')
COMFY_GAP_AFTER_CLASS = 400  # max bytes between a class_type value and a following "inputs"
COMFY_GAP_AFTER_INPUTS = 4000  # max bytes between "inputs" and a following "class_type":

# Hints that C2PA data is present (used only if the C2PA reader is unavailable or fails).
C2PA_HINT = re.compile(rb"jumb.{0,64}?c2pa|c2pa\.(?:claim|signature|actions)", re.S)

TEXT_MARKERS: list[_Marker] = [
    _txt("iptc_trained", "ai", r"\btrainedAlgorithmicMedia\b",
         "IPTC digital source type says this was made by generative AI (trainedAlgorithmicMedia)", 1.0),
    _txt("iptc_composite", "ai", r"\bcompositeWithTrainedAlgorithmicMedia\b",
         "IPTC digital source type says AI-generated content was mixed in (compositeWithTrainedAlgorithmicMedia)", 1.0),
    _txt("openai", "ai", r"\bOpenAI\b|\bDALL[·\-\s]?E\b|\bChatGPT\b|\bSora\b|\bGPT-4o\b|\bgpt-image-1\b", "Metadata names an OpenAI tool (DALL·E / ChatGPT / Sora)", 0.9),
    _txt("google_ai", "ai", r"\bGemini\b|\bSynthID\b|\bGoogle\s+(?:Imagen|Veo)(?:\s*\d+(?:\.\d+)?)?\b|"
         r"\b(?:Imagen|Veo)\s*\d+(?:\.\d+)?\s+(?:by\s+)?Google\b",
         "Metadata names a Google AI model (Gemini / Imagen / Veo)", 0.9),
    _txt("midjourney", "ai", r"\bMidjourney\b", "Metadata names Midjourney", 0.9, re.I),
    _txt("firefly", "ai", r"\bFirefly\b", "Metadata names Adobe Firefly", 0.9),
    _txt("stable_diffusion", "ai", r"\bStable Diffusion\b|\bstable-diffusion\b|\bsd_xl_base\b|\bSDXL\b|\bDreamStudio\b|\bComfyUI\b|\bAUTOMATIC1111\b|\bInvokeAI\b|\bFooocus\b",
         "Metadata names a Stable Diffusion tool", 0.9),
    _txt("flux", "ai", r"\bFLUX(?:\.1)?\b|\bFlux\.1\b|\bBlack Forest Labs\b", "Metadata names FLUX (Black Forest Labs)", 0.9),
    _txt("ai_video", "ai", r"\bRunway(?:ML)?\b|\bKling\b|\bPika\b|\bHeyGen\b|\bSynthesia\b|\bLuma Dream Machine\b",
         "Metadata names a known AI video tool", 0.9),
    _txt("ai_image", "ai", r"\bNovelAI\b|\bLeonardo(?:\.Ai)?\b|\bIdeogram\b|\bGrok\b|\bRecraft\b|\bBing Image Creator\b|\bImage Creator from Designer\b",
         "Metadata names a known AI image tool", 0.9),
    _txt("faceswap", "ai", r"\bDeepFaceLab\b|\bDeepFaceLive\b|\bFaceFusion\b|\bReface\b|\bFaceSwap\b|\broop(?:-unleashed)?\b",
         "Metadata names a known face-swap tool", 0.9, re.I),
    _txt("editor", "edit",
         r"\bPhotoshop\b|\bLightroom\b|\bGIMP\b|\bSnapseed\b|\bPixelmator\b|\bAffinity (?:Photo|Designer)\b|\bCanva\b|"
         r"\bCapCut\b|\bPremiere\b|\bAfter Effects\b|\bDaVinci Resolve\b|\bFinal Cut\b|\biMovie\b|\bPicsart\b|"
         r"\bFacetune\b|\bMeitu\b|\bPhotopea\b|\bpaint\.net\b|\bLuminar\b|\bCapture One\b|\bDarktable\b|\bRawTherapee\b",
         "Metadata names editing software", 0.6, re.I),
]


def read_window(path: str | Path, window: int = RAW_WINDOW) -> bytes:
    """Return the first and last `window` bytes of the file (the whole file if it is small)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        if size <= 2 * window:
            return f.read()
        head = f.read(window)
        f.seek(size - window)
        return head + b"\0" + f.read(window)


def _dedupe(hits: list[Hit]) -> list[Hit]:
    seen: set[str] = set()
    out = []
    for h in hits:
        if h.key not in seen:
            seen.add(h.key)
            out.append(h)
    return out


def scan_bytes(data: bytes, field: str = "file bytes") -> list[Hit]:
    """Structured generator markers in raw bytes (or in decoded text from `field`)."""
    hits = []
    for m in RAW_MARKERS:
        match = m.pattern.search(data)
        if match:
            raw = data[match.start() : match.start() + 2 * SNIPPET_MAX].decode("utf-8", "replace")
            snippet = _snippet(raw, 0)
            hits.append(Hit(m.kind, m.key, _describe(m.text, field, snippet), m.weight, field, snippet))
    return _dedupe(hits)


def scan_generator_metadata(data: bytes, field: str, key: str) -> list[Hit]:
    """Recognize generator settings only in properties where generators actually store them."""
    lower = key.lower()
    if lower not in {"parameters", "prompt", "workflow", "usercomment"}:
        return []
    start = _a1111_match(data)
    if start is not None:
        snippet = _snippet(data[start:].decode("utf-8", "replace"), 0)
        return [Hit("ai", "sd_params", _describe("Stable Diffusion generation settings found", field, snippet),
                    1.0, field, snippet)]
    if lower in {"prompt", "workflow"}:
        start = _comfy_match(data)
        if start is not None:
            snippet = _snippet(data[start:].decode("utf-8", "replace"), 0)
            return [Hit("ai", "comfyui", _describe("ComfyUI workflow embedded in the file", field, snippet),
                        1.0, field, snippet)]
    return []


def _a1111_match(data: bytes) -> int | None:
    """Start of an A1111 parameters block (the prompt, i.e. 0) or None. Linear time.

    Same language as the former `(?s).+\\nSteps:\\s*\\d+,\\s*Sampler:\\s*[^,\\n]+,.*?CFG scale:\\s*[\\d.]+,.*?Seed:\\s*\\d+`:
    a non-empty prompt, a "Steps: N, Sampler: X," line, then "CFG scale: N," and later "Seed: N". The
    earliest header, then the earliest CFG after it, then any Seed after that is a match iff one exists.
    """
    head = A1111_HEAD_RE.search(data, 1)  # pos 1: the prompt before "\nSteps:" must be non-empty
    if head is None:
        return None
    cfg = A1111_CFG_RE.search(data, head.end())
    if cfg is None or A1111_SEED_RE.search(data, cfg.end()) is None:
        return None
    return 0


def _comfy_match(data: bytes) -> int | None:
    """Start of the first ComfyUI node marker or None. Linear time (bounded windows, C-level find)."""
    best: int | None = None
    # "class_type": "Name" ... (at most 400 bytes) ... "inputs"
    for m in COMFY_CLASS_RE.finditer(data):
        if data.find(COMFY_INPUTS, m.end(), m.end() + COMFY_GAP_AFTER_CLASS + len(COMFY_INPUTS)) != -1:
            best = m.start()
            break
    # "inputs" ... (at most 4000 bytes) ... "class_type":  -- searched from the class_type side, one
    # bounded C-level find per key, so many "inputs" keys without a class_type cost nothing extra.
    for m in COMFY_CLASS_KEY_RE.finditer(data):
        lo = max(0, m.start() - COMFY_GAP_AFTER_INPUTS - len(COMFY_INPUTS))
        i = data.find(COMFY_INPUTS, lo, m.start())
        if i != -1:
            best = i if best is None else min(best, i)
            break
    return best


def scan_raw(path: str | Path) -> list[Hit]:
    return scan_bytes(read_window(path))


def has_c2pa_hint(data: bytes) -> bool:
    return bool(C2PA_HINT.search(data))


def scan_text(values: list[tuple[str, str]]) -> list[Hit]:
    """Match brand/tool names in `(field, value)` pairs. Only pass fields that name the creating software."""
    hits = []
    for field, v in values:
        if not v:
            continue
        for m in TEXT_MARKERS:
            match = m.pattern.search(v)
            if not match:
                continue
            key = f"edit:{match.group(0).lower()}" if m.kind == "edit" else m.key
            snippet = _snippet(v, match.start())
            hits.append(Hit(m.kind, key, _describe(m.text, field, snippet), m.weight, field, snippet))
    return _dedupe(hits)


def _describe(text: str, field: str, snippet: str) -> str:
    return f"{text}. Found in {field}: \u201c{snippet}\u201d" if field else f"{text}: \u201c{snippet}\u201d"


def _snippet(value: str, at: int, width: int = SNIPPET_MAX) -> str:
    """At most `width` characters of `value` starting a little before position `at`, whitespace collapsed."""
    start = max(0, at - 20)
    window = value[start : at + 4 * width]
    text = ("\u2026" if start else "") + " ".join("".join(c if c.isprintable() else " " for c in window).split())
    if len(text) > width or start + len(window) < len(value):
        text = text[: width - 1].rstrip() + "\u2026"
    return text
