"""Video probing and frame sampling with PyAV (bundled FFmpeg)."""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

log = logging.getLogger("banthefake.video")

SAMPLE_FRAMES = 8
MAX_PIXELS = 8192 * 4320  # refuse to decode frames larger than 8K
MAX_FRAME_SIDE = 1024  # sampled frames are downscaled to this long side
MAX_DECODE_PER_SEEK = 240  # frames decoded after a seek before settling for the last one
TIME_BUDGET_S = 20.0
META_KEYS_TOOL = ("encoder", "software", "comment", "description", "title", "artist", "copyright", "make", "model")


@dataclass
class VideoInfo:
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    codec: str = ""
    has_video: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
    frames: list[Image.Image] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    error: str | None = None

    @property
    def make(self) -> str:
        return _first(self.metadata, ("com.apple.quicktime.make", "make", "manufacturer"))

    @property
    def model(self) -> str:
        return _first(self.metadata, ("com.apple.quicktime.model", "model"))

    def tool_strings(self) -> list[str]:
        out = []
        for k, v in self.metadata.items():
            if any(t in k.lower() for t in META_KEYS_TOOL):
                out.append(v)
        return out


def _first(meta: dict[str, str], keys: tuple[str, ...]) -> str:
    lower = {k.lower(): v for k, v in meta.items()}
    for k in keys:
        if lower.get(k):
            return lower[k]
    return ""


def probe(path: str | Path, sample: int = SAMPLE_FRAMES) -> VideoInfo:
    """Read container metadata and sample up to `sample` frames spread over the video."""
    import av

    info = VideoInfo()
    try:
        container = av.open(str(path))
    except Exception as exc:
        info.error = f"could not open video ({type(exc).__name__})"
        return info
    with container:
        info.metadata = {str(k)[:64]: str(v)[:1024] for k, v in (container.metadata or {}).items()}
        if not container.streams.video:
            info.error = "no video track"
            return info
        stream = container.streams.video[0]
        for k, v in (stream.metadata or {}).items():
            info.metadata.setdefault(str(k)[:64], str(v)[:1024])
        info.has_video = True
        ctx = stream.codec_context
        info.codec = ctx.name or ""
        info.width, info.height = ctx.width or None, ctx.height or None
        if container.duration:
            info.duration_s = container.duration / av.time_base
        elif stream.duration and stream.time_base:
            info.duration_s = float(stream.duration * stream.time_base)
        if info.width and info.height and info.width * info.height > MAX_PIXELS:
            info.error = f"resolution too large to decode ({info.width}x{info.height})"
            return info
        if sample > 0:
            try:
                _sample(container, stream, info, sample)
            except Exception as exc:
                log.info("frame sampling stopped: %s", type(exc).__name__)
                if not info.frames:
                    info.error = f"could not decode frames ({type(exc).__name__})"
    return info


def _sample(container, stream, info: VideoInfo, n: int) -> None:
    """Seek to the keyframe before each target time, then decode forward to the target.

    Decoding forward is capped (frames per seek and a total time budget) so long GOPs or huge
    files cannot stall the server; when the cap is hit the last decoded frame is used.
    """
    stream.thread_type = "AUTO"
    deadline = time.monotonic() + TIME_BUDGET_S
    dur = info.duration_s or 0.0
    big = (info.width or 0) * (info.height or 0) > 1920 * 1088
    cap = MAX_DECODE_PER_SEEK // 4 if big else MAX_DECODE_PER_SEEK
    targets = [dur * (i + 0.5) / n for i in range(n)] if dur > 0 else [0.0]
    seen: set[int] = set()
    for t in targets:
        if time.monotonic() > deadline:
            break
        offset = int(t / stream.time_base) if stream.time_base else 0
        if stream.start_time is not None:
            offset += stream.start_time
        container.seek(offset, stream=stream, backward=True, any_frame=False)
        picked = None
        for i, frame in enumerate(container.decode(stream)):
            picked = frame
            if (frame.time is not None and frame.time >= t - 0.02) or i + 1 >= cap or time.monotonic() > deadline:
                break
        if picked is None:
            continue
        key = picked.pts if picked.pts is not None else len(seen)
        if key in seen:
            continue
        seen.add(key)
        info.frames.append(_to_image(picked))
        info.timestamps.append(round(float(picked.time or 0.0), 3))


def _to_image(frame) -> Image.Image:
    w, h = frame.width, frame.height
    scale = min(1.0, MAX_FRAME_SIDE / max(w, h))
    if scale < 1.0:
        w, h = max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)
    return frame.reformat(width=w, height=h, format="rgb24").to_image()
