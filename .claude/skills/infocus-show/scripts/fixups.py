"""Fix-up clips for switcher mistakes, made from the recording's own frames (ProRes 4444, 1920x1080).

    uv run --with numpy --with pillow python fixups.py grab  <recording> <frame> <out.png>
    uv run --with numpy --with pillow python fixups.py hold  <recording> <frame> <frames> <out_dir>
    uv run --with numpy --with pillow python fixups.py patch <recording> <clean_frame> <x0,y0,x1,y1> <frames> <out_dir>
    uv run --with numpy --with pillow python fixups.py under <overlay.mov> <recording> <clean_frame> <x0,y0,x1,y1> <out_dir>

- grab:  one frame by its index in the recording. Never seek by time: a time seek once landed on the wrong frame.
- hold:  a full-frame still for <frames> frames at the recording's rate. Covers a full-screen card flash while the
         anchor keeps talking (cut the flash, hold a clean two-shot frame over what's left).
- patch: a clean plate of rectangle x0,y0-x1,y1 from <clean_frame>, feathered inside the rectangle and transparent
         (pixels zeroed) outside it. Covers a logo bug or graphic the switcher fired early over a clean set wall.
- under: a package overlay composited over a clean patch, at the overlay's own rate and length. Use when the old
         graphic's last frames outlast the fresh one.
hold/patch/under write into <out_dir> with the source frame and length in the name (e.g. hold_f6107_29f.mov), so a
remade clip always gets a new name and never gets mixed up with the one it replaces. Each prints the file it wrote.
"""
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import H, W, fps_value, probe, seek_args  # noqa: E402

FEATHER = 6           # px, inside the patch rectangle
PRORES = ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p12le", "-alpha_bits", "16",
          "-vendor", "apl0"]


def video_info(path):
    info = probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    return v["r_frame_rate"], int(v["width"]), int(v["height"])


def grab_rgb(recording, frame):
    """The recording's frame <frame> as an RGB uint8 array (exact frame, by index)."""
    rate, w, h = video_info(recording)
    pre, k = seek_args(frame, fps_value(rate))
    out = subprocess.run(["ffmpeg", "-v", "error", *pre, "-i", recording, "-vf", f"select=eq(n\\,{k})",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    if len(out) != w * h * 3:
        raise RuntimeError(f"frame {frame} is past the end of {recording}")
    return np.frombuffer(out, np.uint8).reshape(h, w, 3)


def grab(recording, frame, out_png):
    Image.fromarray(grab_rgb(recording, frame)).save(out_png)
    return out_png


def _encode_still(rgba, rate, frames, out):
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "still.png")
        Image.fromarray(rgba, "RGBA").save(png)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", rate, "-i", png,
                        "-frames:v", str(frames), *PRORES, "-r", rate, out], check=True)
    return out


def hold(recording, frame, frames, out_dir):
    rate, _, _ = video_info(recording)
    rgb = grab_rgb(recording, frame)
    rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
    os.makedirs(out_dir, exist_ok=True)
    return _encode_still(rgba, rate, frames, os.path.join(out_dir, f"hold_f{frame}_{frames}f.mov"))


def parse_rect(text):
    x0, y0, x1, y1 = (int(v) for v in text.split(","))
    if not (0 <= x0 < x1 <= W and 0 <= y0 < y1 <= H):
        raise ValueError(f"rectangle {text} must be x0,y0,x1,y1 inside {W}x{H}")
    return x0, y0, x1, y1


def patch_rgba(rgb, rect, feather=FEATHER):
    """Clean plate of rect from rgb: alpha ramps 0→255 over <feather> px inside the rect edge, 0 outside it,
    and colour zeroed wherever alpha is 0."""
    x0, y0, x1, y1 = rect
    h, w = rgb.shape[:2]
    ys, xs = np.mgrid[0:h, 0:w]
    edge = np.minimum.reduce([xs - x0 + 1, x1 - xs, ys - y0 + 1, y1 - ys]).astype(np.float32)
    alpha = np.clip(edge / feather, 0, 1)
    rgba = np.dstack([rgb, (alpha * 255).round().astype(np.uint8)])
    rgba[rgba[..., 3] == 0] = 0
    return rgba


def patch(recording, clean_frame, rect, frames, out_dir):
    rate, _, _ = video_info(recording)
    rgba = patch_rgba(grab_rgb(recording, clean_frame), rect)
    x0, y0, x1, y1 = rect
    os.makedirs(out_dir, exist_ok=True)
    name = f"patch_f{clean_frame}_{x0}-{y0}-{x1}-{y1}_{frames}f.mov"
    return _encode_still(rgba, rate, frames, os.path.join(out_dir, name))


def under(overlay, recording, clean_frame, rect, out_dir):
    """overlay OVER patch, frame by frame (straight alpha), at the overlay's rate and length."""
    rate, w, h = video_info(overlay)
    base = patch_rgba(grab_rgb(recording, clean_frame), rect).astype(np.float32) / 255
    stem = os.path.splitext(os.path.basename(overlay))[0].replace(" ", "_")
    os.makedirs(out_dir, exist_ok=True)
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", overlay, "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                           stdout=subprocess.PIPE)
    tmp = os.path.join(out_dir, f".under_{os.getpid()}.mov")
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}",
                            "-framerate", rate, "-i", "-", *PRORES, "-r", rate, tmp], stdin=subprocess.PIPE)
    n = 0
    while True:
        buf = dec.stdout.read(w * h * 4)
        if len(buf) < w * h * 4:
            break
        top = np.frombuffer(buf, np.uint8).reshape(h, w, 4).astype(np.float32) / 255
        ta, ba = top[..., 3:], base[..., 3:]
        a = ta + ba * (1 - ta)
        rgb = (top[..., :3] * ta + base[..., :3] * ba * (1 - ta)) / np.maximum(a, 1e-6)
        out = (np.dstack([rgb, a]) * 255).round().astype(np.uint8)
        out[out[..., 3] == 0] = 0
        enc.stdin.write(out.tobytes())
        n += 1
    enc.stdin.close()
    if enc.wait() or dec.wait():
        raise RuntimeError(f"under: ffmpeg failed on {overlay}")
    final = os.path.join(out_dir, f"under_{stem}_f{clean_frame}_{n}f.mov")
    os.replace(tmp, final)
    return final


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "grab" and len(args) == 3:
            out = grab(args[0], int(args[1]), args[2])
        elif cmd == "hold" and len(args) == 4:
            out = hold(args[0], int(args[1]), int(args[2]), args[3])
        elif cmd == "patch" and len(args) == 5:
            out = patch(args[0], int(args[1]), parse_rect(args[2]), int(args[3]), args[4])
        elif cmd == "under" and len(args) == 5:
            out = under(args[0], args[1], int(args[2]), parse_rect(args[3]), args[4])
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        print(f"fixups {cmd}: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"file": out}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
