"""Checks a rendered show against its edl/layout: loudness per segment, a sheet of every segment and card,
and frame-by-frame strips where each cover meets the old baked-in overlay's in and out.

    uv run --with pillow python check_render.py <show_dir> [render.mp4]
Writes <show_dir>/check/: segments.jpg, covers_<id>.jpg, loudness.json; prints a loudness table.
A segment's audio passes only when its true peak sits at the ceiling (-6.0 dBFS) within ±0.3 dB: under it is a
fail too (the producer's standard is -6 everywhere, packages and throws included).
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import fps_value, load_edl  # noqa: E402

SHEET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sheet.py")


def loudness(video, a, b):
    r = subprocess.run(["ffmpeg", "-nostats", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", video, "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", r)
    p = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", r)
    return (float(i[-1]) if i else None), (float(p[-1]) if p else None)


PEAK_TOLERANCE = 0.3


def peak_ok(peak, target=-6.0, tol=PEAK_TOLERANCE):
    return peak is not None and abs(peak - target) <= tol + 1e-9


def loudness_rows(video, layout, edl):
    """Per program segment: integrated loudness and true peak in the render, and whether the peak passes.
    Placeholders (silent slates) are skipped; segments under 1 s can't be measured and are reported as such."""
    fps = fps_value(edl["fps"])
    target = float(edl.get("audio", {}).get("peak_dbfs", -6.0))
    rows = []
    for s in layout["segments"]:
        if s["kind"] == "placeholder":
            continue
        a, b = s["rec_in"] / fps, s["rec_out"] / fps
        if b - a <= 1:
            rows.append({"segment": s["id"], "kind": s["kind"], "lufs": None, "peak": None, "ok": True,
                         "note": "too short to measure"})
            continue
        lufs, peak = loudness(video, a + 0.1, b - 0.1)
        rows.append({"segment": s["id"], "kind": s["kind"], "lufs": lufs, "peak": peak, "ok": peak_ok(peak, target)})
    return rows


def sheet(video, out, frames, *extra):
    subprocess.run([sys.executable, SHEET, video, out, ",".join(str(f) for f in frames), *extra], check=True,
                   capture_output=True)
    return out


def main():
    show_dir = sys.argv[1]
    edl = load_edl(show_dir)
    video = sys.argv[2] if len(sys.argv) > 2 else os.path.join(show_dir, f"{edl['show']} (preview).mp4")
    layout = json.load(open(os.path.join(show_dir, "layout.json")))
    fps = fps_value(edl["fps"])
    out = os.path.join(show_dir, "check")
    os.makedirs(out, exist_ok=True)

    rows = loudness_rows(video, layout, edl)
    print(f"{'segment':22} {'kind':12} {'LUFS':>6} {'peak':>6}  ok")
    for r in rows:
        lufs = "" if r["lufs"] is None else r["lufs"]
        peak = "" if r["peak"] is None else r["peak"]
        print(f"{r['segment']:22} {r['kind']:12} {lufs:>6} {peak:>6}  {'yes' if r['ok'] else 'NO'}")
    with open(os.path.join(out, "loudness.json"), "w") as f:
        json.dump(rows, f, indent=1)

    mids = [(s["rec_in"] + s["rec_out"]) // 2 for s in layout["segments"]]
    mids += [c["rec_in"] + min(int(6 * fps), (c["rec_out"] - c["rec_in"]) // 2) for c in layout["cards"]]
    print(sheet(video, os.path.join(out, "segments.jpg"), sorted(mids), "--width", "480", "--cols", "4"))

    src_segs = {s["id"]: s for s in edl["segments"] if s["kind"] == "source"}
    rec_of = {s["id"]: s["rec_in"] for s in layout["segments"]}
    for o in edl.get("overlays", []):
        cov = o.get("covers")
        if not cov:
            continue
        def to_rec(src):
            for sid, s in src_segs.items():
                if s["in"] <= src < s["out"]:
                    return rec_of[sid] + src - s["in"]
            return None
        first, last = to_rec(cov[0]), to_rec(cov[1])
        if first is None or last is None:
            print(f"{o['id']}: old overlay f{cov[0]}–f{cov[1]} is not fully inside a chosen take; check it by eye")
            continue
        frames = [f for f in list(range(first - 2, first + 6)) + list(range(last - 5, last + 3)) if f >= 0]
        crop = o.get("check_crop", "0,780,1920,240" if "dual" in json.dumps(o) or "lower" in json.dumps(o) else "0,0,960,400")
        print(sheet(video, os.path.join(out, f"covers_{o['id']}.jpg"), frames, "--crop", crop, "--width", "640", "--cols", "4"))


if __name__ == "__main__":
    main()
