"""Anchor foregrounds for the side-camera card segments, so the fresh TV cards sit *behind* the anchors.

On the Left/Right sets an anchor's shoulder or hair often crosses the TV. For every frame of each card segment:
Apple Vision person matte -> keep only the part over the TV screen -> the recording's own pixels with that alpha
(ProRes 4444, 1920x1080, transparent everywhere else). Goes on the track above the cards.
A TV left on the switcher's blue key is keyed out of the anchor's soft edges, and their blue spill neutralised.

    uv run --with numpy --with opencv-python python foreground.py <show_dir> [--only <segment id>]
"""
import os
import subprocess
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import H, SCREENS, W, card_segments, ensure_matter, foreground_file, fps_value, load_edl, segment_source  # noqa: E402
from tv_key import blueness, despill, on_key  # noqa: E402


def alpha_for(frame, matte, rect):
    m = matte.astype(np.float32) / 255
    if on_key(frame, rect, matte):                                          # TV left on the blue key: inside the
        edge = np.clip(m / 0.1, 0, 1) * (1 - blueness(frame))              # matte, the color says how much is anchor
    else:
        bright = np.clip((frame.max(2).astype(np.float32) - 30) / 22, 0, 1)   # screen ink is ~15; skin/clothes are well above
        edge = m * bright
    core = cv2.erode((m > 0.9).astype(np.uint8), np.ones((13, 13), np.uint8)).astype(np.float32)
    core = cv2.GaussianBlur(core, (7, 7), 0)                                # solid body even in dark folds
    a = cv2.GaussianBlur(np.maximum(core, edge), (3, 3), 0)
    x, y, w, h = rect
    keep = np.zeros_like(a)
    keep[y:y + h, x:x + w] = 1                                             # only where a card is under the anchor
    return a * keep


def build(src, fps, f_in, f_out, rect, out):
    rate = fps_value(fps)
    n = f_out - f_in
    t0, t1 = (f_in - 0.25) / rate, (f_out - 0.25) / rate
    matte = subprocess.Popen([ensure_matter(), src, f"{t0:.6f}", f"{t1:.6f}"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    raw = subprocess.Popen(["ffmpeg", "-v", "error", "-ss", f"{t0:.6f}", "-i", src, "-frames:v", str(n),
                            "-vf", f"scale={W}:{H}:flags=lanczos", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}",
                            "-r", str(fps), "-i", "-", "-c:v", "prores_ks", "-profile:v", "4444",
                            "-pix_fmt", "yuva444p10le", "-alpha_bits", "16", "-vendor", "apl0", out],
                           stdin=subprocess.PIPE)
    done = 0
    for _ in range(n):
        fb, mb = raw.stdout.read(W * H * 3), matte.stdout.read(W * H)
        if len(fb) < W * H * 3 or len(mb) < W * H:
            break
        f = np.frombuffer(fb, np.uint8).reshape(H, W, 3)
        mt = np.frombuffer(mb, np.uint8).reshape(H, W)
        a8 = (alpha_for(f, mt, rect) * 255 + 0.5).astype(np.uint8)
        if on_key(f, rect, mt):
            f = despill(f, a8 / 255)
        enc.stdin.write(np.dstack([f * (a8 > 0)[..., None], a8]).tobytes())   # blank RGB under zero alpha: small file
        done += 1
    enc.stdin.close()
    enc.wait()
    raw.kill()
    matte.kill()
    if done != n:
        raise SystemExit(f"{os.path.basename(out)}: wrote {done} of {n} frames")
    print(f"{os.path.basename(out)}: {done} frames", flush=True)


def main():
    show_dir = sys.argv[1]
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    edl = load_edl(show_dir)
    os.makedirs(os.path.join(show_dir, "Foreground"), exist_ok=True)
    for seg in card_segments(edl):
        if only and seg["id"] != only:
            continue
        src, off = segment_source(edl, seg)
        build(src, edl["fps"], seg["in"] + off, seg["out"] + off, SCREENS[seg["cam"]], foreground_file(show_dir, seg))


if __name__ == "__main__":
    main()
