"""Issue: on long-GOP videos (keyframes far apart) every seek fell back to the first keyframe, so all
sampled frames came from the first seconds of the clip."""

import shutil
import subprocess

import pytest

from app.scan import model, video
from tests import media

DURATION = 30.0


@pytest.fixture(scope="module")
def long_gop(tmp_path_factory):
    """30 s, a single keyframe. Built with ffmpeg if installed, else with PyAV."""
    path = tmp_path_factory.mktemp("lg") / "longgop.mp4"
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
             "-t", str(int(DURATION)), "-g", "100000", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            check=True,
        )
    else:
        path.write_bytes(media.mp4(seconds=DURATION, fps=30, size=(320, 240), gop=100000))
    return path


def keyframes(path) -> int:
    import av

    with av.open(str(path)) as c:
        return sum(1 for p in c.demux(c.streams.video[0]) if p.is_keyframe)


def test_long_gop_fixture_has_sparse_keyframes(long_gop):
    assert keyframes(long_gop) <= 2  # libx264: exactly 1; PyAV mpeg4 fallback may add a scene-cut keyframe


def test_long_gop_samples_spread_over_whole_duration(long_gop):
    v = video.probe(long_gop)
    assert v.error is None and v.duration_s == pytest.approx(DURATION, abs=0.2)
    assert len(v.frames) == video.SAMPLE_FRAMES, v.timestamps
    assert v.timestamps == sorted(v.timestamps)
    assert v.timestamps[0] < DURATION / 8 and v.timestamps[-1] > 0.85 * DURATION
    # every sample lands close to its evenly spaced target
    targets = [DURATION * (i + 0.5) / video.SAMPLE_FRAMES for i in range(video.SAMPLE_FRAMES)]
    assert all(abs(ts - t) < 1.0 for ts, t in zip(v.timestamps, targets))


def test_long_gop_respects_time_budget(long_gop, monkeypatch):
    monkeypatch.setattr(video, "TIME_BUDGET_S", 0.0)
    v = video.probe(long_gop)
    assert len(v.frames) <= 1  # budget exhausted: stops early instead of stalling


def test_model_block_reports_coverage(client, long_gop, monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "predict", lambda images: [0.1] * len(images))
    res = client.post("/api/scan", files={"file": ("v.mp4", long_gop.read_bytes())})
    m = res.json()["model"]
    cov = m["coverage"]
    assert cov["duration_s"] == pytest.approx(DURATION, abs=0.2)
    assert cov["first_s"] < DURATION / 8 and cov["last_s"] > 0.85 * DURATION
    assert len(m["frame_timestamps"]) == m["frames_checked"] == video.SAMPLE_FRAMES
    text = next(s["text"] for s in res.json()["signals"] if s["kind"] == "model")
    assert f"of {DURATION:.1f} s" in text


def test_partial_coverage_is_reported(client, long_gop, monkeypatch):
    monkeypatch.setenv("BTF_MODEL", "auto")
    monkeypatch.setattr(model, "predict", lambda images: [0.1] * len(images))
    real_probe = video.probe

    def early_only(path, sample=video.SAMPLE_FRAMES):
        v = real_probe(path, sample)
        keep = [i for i, t in enumerate(v.timestamps) if t < 10]
        v.frames = [v.frames[i] for i in keep]
        v.timestamps = [v.timestamps[i] for i in keep]
        return v

    monkeypatch.setattr(video, "probe", early_only)
    body = client.post("/api/scan", files={"file": ("v.mp4", long_gop.read_bytes())}).json()
    assert any("only reached" in s["text"] and "so the rest was not checked" in s["text"] for s in body["signals"])
