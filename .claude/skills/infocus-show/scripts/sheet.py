"""Frame-exact contact sheet, for checking edit points, cover timing and renders.

    uv run --with pillow python sheet.py <video> <out.jpg> <frames> [--crop x,y,w,h] [--width 480] [--cols 6]
<frames>: comma list and/or ranges in the video's own frame numbers, e.g. "3520-3526,4684,6258-6262".
Every tile is labelled with its frame number and time.
"""
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import fps_value, probe  # noqa: E402


def parse_frames(spec):
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = (int(v) for v in part.split("-"))
            out += range(a, b + 1)
        elif part:
            out.append(int(part))
    return out


def main():
    video, out, spec = sys.argv[1:4]
    args = sys.argv[4:]
    opt = lambda k, d: args[args.index(k) + 1] if k in args else d   # noqa: E731
    width, cols, crop = int(opt("--width", 480)), int(opt("--cols", 6)), opt("--crop", None)
    v = next(s for s in probe(video)["streams"] if s["codec_type"] == "video")
    fps = fps_value(v["r_frame_rate"])
    vf = ["scale=1920:1080"]
    if crop:
        x, y, w, h = crop.split(",")
        vf.append(f"crop={w}:{h}:{x}:{y}")
    vf.append(f"scale={width}:-2")
    tiles = []
    with tempfile.TemporaryDirectory() as tmp:
        for n in parse_frames(spec):
            p = os.path.join(tmp, f"{n}.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{(n + 0.25) / fps:.5f}", "-i", video,
                            "-frames:v", "1", "-vf", ",".join(vf), p], check=True)
            im = Image.open(p).convert("RGB")
            d = ImageDraw.Draw(im)
            label = f"f{n}  {int(n / fps // 60)}:{n / fps % 60:05.2f}"
            d.rectangle([0, 0, 7 * len(label) + 4, 13], fill="black")
            d.text((2, 1), label, fill="yellow")
            tiles.append(im)
    tw, th = tiles[0].size
    sheet = Image.new("RGB", (cols * tw, ((len(tiles) + cols - 1) // cols) * th), "white")
    for i, im in enumerate(tiles):
        sheet.paste(im, ((i % cols) * tw, (i // cols) * th))
    sheet.save(out, quality=88)
    print(out)


if __name__ == "__main__":
    main()
