"""Poster candidates: frames from the finished render where both anchors look natural, while the dual third is
fully on screen (not animating in or out).

    uv run --with numpy --with opencv-python --with pillow python poster_pick.py <show_dir> <render> <out_dir>

Samples every 3rd frame of the dual third's fully-on window (with no dual third: the first two-shot take, from 1 s
in, for 4 s), scores every anchor's face with Apple Vision landmarks (eyes open, mouth at rest, facing the camera;
the worst face sets the frame's score) and writes <out_dir>/candidates.jpg (the best 6, labelled with their time in
the render) and candidates.json ([{seconds, score, faces}], best first). View the sheet, pick one, then run
poster.py at its seconds.
"""
import json
import math
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ensure_tool, fps_value, load_edl, overlay_file  # noqa: E402

FULL_SHARE = 0.98                             # of the plate's most drawn pixels: fully on, not animating
EVERY = 3                                     # frames between samples
EYES_OPEN = 0.25                              # eye height / width at which an eye counts as fully open
MOUTH_REST = 0.01                             # inner-lip gap (face-box units) still read as a closed mouth
MOUTH_WEIGHT = 4                              # a mouth 0.05 past rest costs as much as a fifth of an eye closing
ANCHORS = 2


def full_window(counts):
    """(first, last) frames of an overlay clip with (nearly) all its pixels drawn, or None. Pixel counts, not the
    bounding box: a dual third's end icons reach full width before its bars fill in."""
    top = max(counts, default=0)
    if not top:
        return None
    full = [i for i, c in enumerate(counts) if c >= FULL_SHARE * top]
    return full[0], full[-1]


def drawn_pixels(path):
    """Per frame of an overlay clip: how many pixels it draws; and its frame rate."""
    import numpy as np
    from common import probe
    v = next(s for s in probe(path)["streams"] if s["codec_type"] == "video")
    w, h = int(v["width"]), int(v["height"])
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                         stdout=subprocess.PIPE)
    counts = []
    while (buf := p.stdout.read(w * h * 4)) and len(buf) == w * h * 4:
        counts.append(int((np.frombuffer(buf, np.uint8)[3::4] > 16).sum()))
    p.wait()
    return counts, fps_value(v["r_frame_rate"])


def frame_score(faces, expected=ANCHORS):
    """How natural the frame's faces look; the worst face decides. -inf when a face is missing."""
    if len(faces) < expected:
        return -math.inf

    def one(f):
        eyes = max(f.get("eyes", -1), 0.0)
        return (min(eyes / EYES_OPEN, 1.0) - MOUTH_WEIGHT * max(f.get("mouth", 0.0) - MOUTH_REST, 0.0)
                - abs(f.get("yaw", 0.0)))
    return min(one(f) for f in faces)


def candidates(scores, k=6, min_gap=30):
    """The best k frames of {frame: score}, at least min_gap frames apart, best first."""
    out = []
    for frame, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        if score == -math.inf:
            break
        if all(abs(frame - f) >= min_gap for f in out):
            out.append(frame)
        if len(out) == k:
            break
    return out


def render_window(full, start, rate, clip_rate):
    """An overlay clip's (first, last) frames as render frames: clip frame k first shows on render frame
    start + ceil(k x rate / clip rate), the way build_ffmpeg conforms it."""
    return tuple(start + math.ceil(f * rate / clip_rate) for f in full)


def window(show_dir, edl, layout):
    """(first, last) render frames to sample."""
    rate = fps_value(edl["fps"])
    placed = {o["id"]: o for o in layout["overlays"]}
    dual = next((o for o in edl.get("overlays", []) if (o.get("render") or {}).get("type") == "dual"
                 and o["id"] in placed), None)
    if dual:
        counts, clip_rate = drawn_pixels(overlay_file(show_dir, dual, edl))
        full = full_window(counts)
        if full:
            return render_window(full, placed[dual["id"]]["rec_in"], rate, clip_rate)
    takes = {s["id"]: s for s in edl["segments"]}
    m = next((s for s in layout["segments"] if takes.get(s["id"], {}).get("cam") == "M"), None)
    if m is None:
        raise SystemExit("no dual third and no two-shot take in this show; pick the poster by eye")
    return m["rec_in"] + round(rate), min(m["rec_out"] - 1, m["rec_in"] + round(5 * rate))


def grab(render, first, last, out_dir):
    """JPEGs of every EVERY-th frame of the render in [first, last], by index: (frames, paths)."""
    frames = list(range(first, last + 1, EVERY))
    pattern = os.path.join(out_dir, "f%05d.jpg")
    expr = f"between(n\\,{first}\\,{last})*not(mod(n-{first}\\,{EVERY}))"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", render, "-vf", f"select={expr}", "-fps_mode", "passthrough",
                    "-q:v", "2", pattern], check=True)
    return frames, [os.path.join(out_dir, f"f{i + 1:05d}.jpg") for i in range(len(frames))]


def sheet(paths, labels, out):
    w, h = 640, 360
    img = Image.new("RGB", (w * 3, h * ((len(paths) + 2) // 3)), "black")
    for i, (p, label) in enumerate(zip(paths, labels)):
        tile = Image.open(p).convert("RGB").resize((w, h))
        ImageDraw.Draw(tile).rectangle((0, 0, 230, 26), fill="black")
        ImageDraw.Draw(tile).text((6, 6), label, fill="white")
        img.paste(tile, ((i % 3) * w, (i // 3) * h))
    img.save(out, quality=90)


def main(argv):
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    show_dir, render, out_dir = argv[1:]
    edl = load_edl(show_dir)
    with open(os.path.join(show_dir, "layout.json")) as f:
        layout = json.load(f)
    rate = fps_value(edl["fps"])
    first, last = window(show_dir, edl, layout)
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        frames, paths = grab(render, first, last, tmp)
        marks = subprocess.run([ensure_tool("face_marks"), *paths], capture_output=True, text=True, check=True)
        faces = {p: json.loads(line)["faces"] for p, line in zip(paths, marks.stdout.splitlines())}
        by_frame = {f: faces.get(p, []) for f, p in zip(frames, paths)}
        scores = {f: frame_score(by_frame[f]) for f in frames}
        best = candidates(scores)
        if not best:
            raise SystemExit(f"no frame in {first}-{last} shows {ANCHORS} faces; pick the poster by eye")
        picks = [{"seconds": round(f / rate, 3), "score": round(scores[f], 3), "faces": by_frame[f]} for f in best]
        sheet([paths[frames.index(f)] for f in best],
              [f"#{i + 1}  {p['seconds']:.2f}s  score {p['score']:.2f}" for i, p in enumerate(picks)],
              os.path.join(out_dir, "candidates.jpg"))
    with open(os.path.join(out_dir, "candidates.json"), "w") as f:
        json.dump(picks, f, indent=1)
    print(f"{len(picks)} candidates from frames {first}-{last}: {os.path.join(out_dir, 'candidates.jpg')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
