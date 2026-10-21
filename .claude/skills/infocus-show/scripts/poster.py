"""The show's YouTube poster: one 1280x720 JPEG frame from the finished render.

    python poster.py <video> <seconds> <out.jpg>
Pick a moment in the open where both anchors are on camera, looking up, with no graphic animating.
"""
import os
import subprocess
import sys


def poster(video, seconds, out):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{float(seconds):.3f}", "-i", video, "-frames:v", "1",
                    "-vf", "scale=1280:720:flags=lanczos", "-q:v", "2", out], check=True)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError(f"no frame at {seconds}s in {video}")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    print(poster(*sys.argv[1:]))
