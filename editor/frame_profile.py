"""Camera class of one frame every N seconds, using the skill's own classifier
(analyze.py, imported unchanged). Runs in a subprocess so the worker itself
doesn't need OpenCV:

    uv run --with numpy --with opencv-python --with pillow --with soundfile \
      python editor/frame_profile.py <skill scripts dir> <package dir> <video> [every_s]

Prints a JSON list of classes: M, L, R, FS, BLACK or OTHER.
"""
from __future__ import annotations

import json
import subprocess
import sys


def main(argv: list[str]) -> int:
    scripts, package, video = argv[0], argv[1], argv[2]
    every = float(argv[3]) if len(argv) > 3 else 2.0
    sys.path.insert(0, scripts)
    import analyze  # noqa: PLC0415 - the skill's module, from its own folder
    import numpy as np  # noqa: PLC0415

    mid = analyze.middle_set(package)
    w, h = analyze.SW, analyze.SH
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-hwaccel", "auto", "-i", video,
                             "-vf", f"fps=1/{every},scale={w}:{h}:flags=area",
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    classes = []
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(w * h * 3)
        if len(buf) < w * h * 3:
            break
        classes.append(analyze.classify(np.frombuffer(buf, np.uint8).reshape(h, w, 3), mid))
    proc.wait()
    print(json.dumps(classes))
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
