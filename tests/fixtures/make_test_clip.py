"""Regenerate both video fixtures from the repository root with this exact command:

.venv/bin/python tests/fixtures/make_test_clip.py tests/fixtures/test_clip.mp4 tests/fixtures/test_clip.mov --inputs tests/fixtures/real_cat_q70_640.jpg tests/fixtures/ai_dalle_cat_q70_640.jpg tests/fixtures/real_bridge.jpg
"""

import argparse

import av
import numpy as np
from PIL import Image


def make_clip(output: str, inputs: list[str]) -> None:
    out = av.open(output, "w")
    stream = out.add_stream("h264", rate=30)
    stream.width, stream.height, stream.pix_fmt = 640, 640, "yuv420p"
    sources = [Image.open(path).convert("RGB").resize((640, 640)) for path in inputs]
    for i in range(150):  # 5 seconds; cycle to the next source every second.
        frame = av.VideoFrame.from_ndarray(np.asarray(sources[(i // 30) % len(sources)]), format="rgb24")
        for packet in stream.encode(frame):
            out.mux(packet)
    for packet in stream.encode():
        out.mux(packet)
    out.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("outputs", nargs="+")
    parser.add_argument("--inputs", nargs="+", required=True)
    args = parser.parse_args()
    for output in args.outputs:
        make_clip(output, args.inputs)


if __name__ == "__main__":
    main()
