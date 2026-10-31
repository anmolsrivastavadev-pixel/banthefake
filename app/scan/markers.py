"""Marker scanning: known AI generators, editing apps and IPTC digital-source-type values.

Two tiers, to keep false positives down:

* RAW markers are long or structured strings that are very unlikely to appear by chance in
  compressed pixel data. They are searched in the raw bytes (first and last few MB only).
* TEXT markers are short or common brand words ("Sora", "Flux", "Gemini", "Photoshop"...). They are
  only matched against metadata text that names a tool (EXIF Software, XMP CreatorTool/softwareAgent,
  PNG text chunks, C2PA claim generator, video container tags), never against raw bytes.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HitKind = Literal["ai", "edit", "info"]

RAW_WINDOW = 4 * 1024 * 1024  # bytes read from the start and from the end of the file


@dataclass(frozen=True)
class Hit:
    kind: HitKind
    key: str  # stable id, used for de-duplication
    text: str
    weight: float


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


RAW_MARKERS: list[_Marker] = [
    _raw("iptc_trained", "ai", rb"digitalsourcetype/trainedAlgorithmicMedia\b|[\">]trainedAlgorithmicMedia[\"<]",
         "IPTC digital source type says this was made by generative AI (trainedAlgorithmicMedia)", 1.0),
    _raw("iptc_composite", "ai", rb"compositeWithTrainedAlgorithmicMedia",
         "IPTC digital source type says AI-generated content was mixed in (compositeWithTrainedAlgorithmicMedia)", 1.0),
    _raw("sd_params", "ai", rb"Steps: \d+, Sampler: ",
         "Stable Diffusion generation settings found (steps / sampler)", 1.0),
    _raw("comfyui", "ai", rb"\"class_type\"\s*:\s*\"[A-Za-z]+[^\"]*\".{0,400}?\"inputs\"|\"inputs\".{0,4000}?\"class_type\"\s*:",
         "ComfyUI workflow embedded in the file", 1.0, re.S),
    _raw("midjourney", "ai", rb"\bMidjourney\b", "Mentions Midjourney", 0.9, re.I),
    _raw("dalle", "ai", rb"\bDALL(?:\xc2\xb7|[-\xb7 ])?E(?:[ -]?[23])?\b", "Mentions DALL·E (OpenAI)", 0.9),
    _raw("chatgpt", "ai", rb"\bChatGPT\b|\bGPT-4o\b|\bgpt-image-1\b", "Mentions ChatGPT / OpenAI image generation", 0.9),
    _raw("firefly", "ai", rb"\bAdobe Firefly\b", "Mentions Adobe Firefly", 0.9),
    _raw("synthid", "ai", rb"\bSynthID\b", "Mentions Google SynthID (AI watermark)", 0.9),
    _raw("stable_diffusion", "ai", rb"\bStable Diffusion\b|\bstable-diffusion\b|\bsd_xl_base\b",
         "Mentions Stable Diffusion", 0.9),
    _raw("faceswap", "ai", rb"\bDeepFaceLab\b|\bFaceFusion\b|\bDeepFaceLive\b|\broop-unleashed\b|\bFaceSwap\b",
         "Mentions a known face-swap tool", 0.9),
    _raw("ai_tools_long", "ai",
         rb"\bNovelAI\b|\bLeonardo\.Ai\b|\bIdeogram\b|\bHeyGen\b|\bSynthesia\b|\bInvokeAI\b|\bFooocus\b|"
         rb"\bAUTOMATIC1111\b|\bDreamStudio\b|\bBing Image Creator\b|\bImage Creator from Designer\b",
         "Mentions a known AI image or video tool", 0.9),
]

# Hints that C2PA data is present (used only if the C2PA reader is unavailable or fails).
C2PA_HINT = re.compile(rb"jumb.{0,64}?c2pa|c2pa\.(?:claim|signature|actions)", re.S)

TEXT_MARKERS: list[_Marker] = [
    _txt("iptc_trained", "ai", r"\btrainedAlgorithmicMedia\b",
         "IPTC digital source type says this was made by generative AI (trainedAlgorithmicMedia)", 1.0),
    _txt("iptc_composite", "ai", r"\bcompositeWithTrainedAlgorithmicMedia\b",
         "IPTC digital source type says AI-generated content was mixed in (compositeWithTrainedAlgorithmicMedia)", 1.0),
    _txt("openai", "ai", r"\bOpenAI\b|\bDALL[·\-\s]?E\b|\bChatGPT\b|\bSora\b", "Metadata names an OpenAI tool (DALL·E / ChatGPT / Sora)", 0.9),
    _txt("google_ai", "ai", r"\bGemini\b|\bImagen\b|\bVeo\b|\bSynthID\b", "Metadata names a Google AI model (Gemini / Imagen / Veo)", 0.9),
    _txt("midjourney", "ai", r"\bMidjourney\b", "Metadata names Midjourney", 0.9, re.I),
    _txt("firefly", "ai", r"\bFirefly\b", "Metadata names Adobe Firefly", 0.9),
    _txt("stable_diffusion", "ai", r"\bStable Diffusion\b|\bSDXL\b|\bComfyUI\b|\bAUTOMATIC1111\b|\bInvokeAI\b|\bFooocus\b",
         "Metadata names a Stable Diffusion tool", 0.9),
    _txt("flux", "ai", r"\bFLUX(?:\.1)?\b|\bFlux\.1\b|\bBlack Forest Labs\b", "Metadata names FLUX (Black Forest Labs)", 0.9),
    _txt("ai_video", "ai", r"\bRunway(?:ML)?\b|\bKling\b|\bPika\b|\bHeyGen\b|\bSynthesia\b|\bLuma Dream Machine\b",
         "Metadata names a known AI video tool", 0.9),
    _txt("ai_image", "ai", r"\bNovelAI\b|\bLeonardo(?:\.Ai)?\b|\bIdeogram\b|\bGrok\b|\bRecraft\b|\bBing Image Creator\b",
         "Metadata names a known AI image tool", 0.9),
    _txt("faceswap", "ai", r"\bDeepFaceLab\b|\bFaceFusion\b|\bReface\b|\bFaceSwap\b|\broop\b",
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


def scan_bytes(data: bytes) -> list[Hit]:
    return _dedupe([Hit(m.kind, m.key, m.text, m.weight) for m in RAW_MARKERS if m.pattern.search(data)])


def scan_raw(path: str | Path) -> list[Hit]:
    return scan_bytes(read_window(path))


def has_c2pa_hint(data: bytes) -> bool:
    return bool(C2PA_HINT.search(data))


def scan_text(values: list[str]) -> list[Hit]:
    """Match brand/tool names in metadata strings that describe the creating software."""
    hits = []
    for v in values:
        for m in TEXT_MARKERS:
            match = m.pattern.search(v)
            if not match:
                continue
            if m.key.startswith("iptc"):
                hits.append(Hit(m.kind, m.key, m.text, m.weight))
            else:
                key = f"edit:{match.group(0).lower()}" if m.kind == "edit" else m.key
                hits.append(Hit(m.kind, key, f"{m.text}: \u201c{_snippet(v, match)}\u201d", m.weight))
    return _dedupe(hits)


def _snippet(value: str, match: re.Match, width: int = 60) -> str:
    v = " ".join(value.split())
    if len(v) <= width:
        return v
    name = match.group(0)
    i = v.find(name)
    start = max(0, i - 20)
    return ("…" if start else "") + v[start : start + width].strip() + "…"
