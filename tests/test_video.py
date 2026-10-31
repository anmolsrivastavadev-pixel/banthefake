import pytest

from app.scan import video
from tests.conftest import FIXTURES


@pytest.mark.parametrize("name", ["test_clip.mp4", "test_clip.mov"])
def test_probe_samples_frames(name):
    v = video.probe(FIXTURES / name)
    assert v.error is None and v.has_video
    assert (v.width, v.height) == (640, 640) and v.codec == "h264"
    assert v.duration_s == pytest.approx(5.0, abs=0.1)
    assert len(v.frames) == video.SAMPLE_FRAMES
    assert v.timestamps == sorted(v.timestamps) and len(set(v.timestamps)) == len(v.timestamps)
    assert v.timestamps[0] < 1.0 and v.timestamps[-1] > 4.0  # spread over the whole clip
    assert all(f.mode == "RGB" and max(f.size) <= video.MAX_FRAME_SIDE for f in v.frames)
    assert any(f == "video tag \u201cencoder\u201d" and "Lavf" in val for f, val in v.tool_fields())


def test_probe_without_sampling():
    v = video.probe(FIXTURES / "test_clip.mp4", sample=0)
    assert v.frames == [] and v.width == 640


def test_probe_garbage_reports_error(tmp_path):
    p = tmp_path / "bad.mp4"
    p.write_bytes(b"\0\0\0\x18ftypisom\0\0\0\0isomavc1" + b"\x07" * 4000)
    v = video.probe(p)
    assert v.error and v.frames == []


def test_huge_resolution_not_decoded(monkeypatch):
    monkeypatch.setattr(video, "MAX_PIXELS", 100 * 100)
    v = video.probe(FIXTURES / "test_clip.mp4")
    assert v.error and "too large" in v.error and v.frames == []


def test_camera_make_model_from_metadata():
    v = video.VideoInfo(metadata={"com.apple.quicktime.make": "Apple", "com.apple.quicktime.model": "iPhone 15 Pro",
                                  "com.apple.quicktime.software": "17.4"})
    assert (v.make, v.model) == ("Apple", "iPhone 15 Pro")
    assert ("video tag \u201ccom.apple.quicktime.software\u201d", "17.4") in v.tool_fields()
