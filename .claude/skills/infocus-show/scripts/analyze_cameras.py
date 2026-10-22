"""First look at a multicam recording: separate camera feeds (M two-shot, L and R side sets) recorded together.

    uv run --with numpy --with opencv-python --with pillow --with soundfile \
      python analyze_cameras.py <show_dir> <recordings.json> <recording id>

<recordings.json> is the worker's show/recordings.json; the recording must be "kind": "multicam". Frame t of the
recording is frame t + offsetFrames of each of its files. Writes <show_dir>/analysis/<recording id>/:
  multicam.json   per feed: camera class per shot run (does the L feed show the L set with its TV?), frozen
                  stretches; per speech run: the words, which feed's own sound is loudest, and mouth movement per
                  face from Apple Vision face landmarks (M left/right anchor, L, R) with the side that seems to be
                  reading
  multicam.md     the same as a report
  sheets/         contact sheets, one row per 4 s: M | L | R side by side, labelled with the common frame
  transcript.txt  whisper transcript (from the M feed's sound) on the recording's timeline
All frame numbers are frames of the recording's timeline (what edl.json "in"/"out" use for this recording).
"""
import json
import os
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import SH, SW, classify, middle_set, speech_runs, tc, transcribe, windows  # noqa: E402
from common import fps_value, probe, seek_args  # noqa: E402

FEEDS = ("M", "L", "R")
MW, MH = 640, 360                  # decode size for classes and sheets (mouths come from mouth_video.swift)
SHEET_EVERY_S = 4.0
MOUTH_HZ = 10                      # mouth-movement samples per second inside a speech run
FROZEN_DIFF = 0.4                  # mean abs frame difference under which a feed counts as frozen (held)
READER_RATIO = 1.3                 # one side's mouth movement must beat the other's by this to name a reader
MIN_MOUTH = 0.3                    # ...and move at least this much (lip-gap spread x100): stillness names nobody
MOUTH_TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "mouth_video")


def feed_length(info, fps):
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    return int(v.get("nb_frames") or 0) or int(float(info["format"]["duration"]) * fps)


def common_length(cams, fps):
    lengths = [feed_length(probe(cams[k]["path"]), fps) - int(cams[k]["offsetFrames"]) for k in FEEDS if k in cams]
    return max(0, min(lengths))


def open_feed(path, offset, n, fps):
    pre, k = seek_args(offset, fps)
    return subprocess.Popen(["ffmpeg", "-v", "error", *pre, "-i", path,
                             "-vf", f"select=gte(n\\,{k}),scale={MW}:{MH}:flags=area", "-frames:v", str(n),
                             "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                            stdout=subprocess.PIPE)


def ensure_mouth_tool():
    """Compile the Apple Vision face-landmark tool on first use."""
    if not os.path.exists(MOUTH_TOOL):
        os.makedirs(os.path.dirname(MOUTH_TOOL), exist_ok=True)
        subprocess.run(["swiftc", "-O", os.path.join(os.path.dirname(MOUTH_TOOL), "..", "mouth_video.swift"),
                        "-o", MOUTH_TOOL], check=True, capture_output=True)
    return MOUTH_TOOL


def mouth_track(path, start_s, end_s, every):
    """[(seconds, [(face centre x, mouth openness), ...]), ...] from Vision face landmarks."""
    out = subprocess.run([ensure_mouth_tool(), path, f"{start_s:.4f}", f"{end_s:.4f}", str(every)],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        v = line.split()
        if len(v) >= 2:
            rows.append((float(v[0]), [(float(v[2 + 2 * i]), float(v[3 + 2 * i])) for i in range(int(v[1]))]))
    return rows


def scan(cams, n, fps, package):
    mid = middle_set(package)
    feeds = [k for k in FEEDS if k in cams]
    procs = {k: open_feed(cams[k]["path"], int(cams[k]["offsetFrames"]), n, fps) for k in feeds}
    every = int(round(SHEET_EVERY_S * fps))
    size = MW * MH * 3
    st = {k: {"cam": [], "diff": []} for k in feeds}
    sheet_rows, prev = [], {}
    for t in range(n):
        row = {}
        for k in feeds:
            buf = procs[k].stdout.read(size)
            if len(buf) < size:
                st[k]["cam"].append("missing")
                st[k]["diff"].append(0.0)
                continue
            f = np.frombuffer(buf, np.uint8).reshape(MH, MW, 3)
            small = cv2.resize(f, (SW, SH), interpolation=cv2.INTER_AREA)
            st[k]["cam"].append(classify(small, mid))
            tiny = cv2.resize(small, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
            st[k]["diff"].append(float(np.abs(tiny - prev[k]).mean()) if k in prev else 0.0)
            prev[k] = tiny
            if t % every == 0:
                row[k] = cv2.resize(f, (320, 180), interpolation=cv2.INTER_AREA)
        if row:
            sheet_rows.append((t, row))
        if t % 3000 == 0 and t:
            print(f"  scanned {t}/{n} frames", flush=True)
    for p in procs.values():
        p.stdout.close()
        p.wait()
    return st, sheet_rows


def feed_runs(st, fps):
    """Per feed: runs of one camera class, and frozen stretches (a held picture: no live camera there)."""
    out = {}
    for k, s in st.items():
        cams = s["cam"]
        runs, start = [], 0
        for t in range(1, len(cams) + 1):
            if t == len(cams) or cams[t] != cams[start]:
                runs.append({"cam": cams[start], "start": start, "end": t})
                start = t
        frozen = windows(np.array(s["diff"]) < FROZEN_DIFF, min_len=int(fps), max_gap=2)
        share = sum(c == k for c in cams) / max(1, len(cams))
        out[k] = {"runs": [r for r in runs if r["end"] - r["start"] >= fps / 2] or runs,
                  "shows_own_set": round(share, 3), "frozen": [[a, b] for a, b in frozen]}
    return out


def mouth_scores(cams, a, b, fps):
    """How much each anchor's mouth moves over frames [a, b): the spread of the inner-lip gap (Vision face
    landmarks) at MOUTH_HZ. M frames two anchors (left and right of frame); L and R one each."""
    scores = {}
    every = max(1, int(round(fps / MOUTH_HZ)))
    for k in FEEDS:
        if k not in cams:
            continue
        off = int(cams[k]["offsetFrames"])
        try:
            rows = mouth_track(cams[k]["path"], (a + off) / fps, (b + off) / fps, every)
        except (OSError, subprocess.CalledProcessError):
            return {}                                       # no Vision here: no mouth cue, the mic still counts
        tracks = {}
        for _, faces in rows:
            for x, gap in faces:
                if gap < 0:
                    continue
                name = (f"M {'left' if x < 0.5 else 'right'}" if k == "M" else k)
                tracks.setdefault(name, []).append(gap)
        for name, gaps in tracks.items():
            if len(gaps) >= 3:
                scores[name] = round(float(np.std(gaps) * 100), 3)
    return scores


def feed_loudness(cams, a, b, fps):
    """dB RMS of each feed's own sound over [a, b): with separate camera mics the reader's feed is loudest."""
    out = {}
    for k in FEEDS:
        if k not in cams or not any(s["codec_type"] == "audio" for s in probe(cams[k]["path"])["streams"]):
            continue
        start = (a + int(cams[k]["offsetFrames"])) / fps
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.4f}", "-t", f"{(b - a) / fps:.4f}",
                              "-i", cams[k]["path"], "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                             capture_output=True).stdout
        x = np.frombuffer(raw, np.float32)
        out[k] = round(float(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)), 1) if len(x) else None
    return out


def reader(mouths, loud):
    """Which side set (L or R) the speaker sits at, if the evidence says so, and why."""
    side = {k: v for k, v in mouths.items() if k in ("L", "R")}
    if len(side) == 2:
        hi, lo = sorted(side, key=side.get, reverse=True)
        if side[hi] >= MIN_MOUTH and side[hi] >= READER_RATIO * max(side[lo], 1e-3):
            return hi, "mouth movement"
    levels = {k: v for k, v in loud.items() if k in ("L", "R") and v is not None}
    if len(levels) == 2 and abs(levels["L"] - levels["R"]) >= 3:
        return max(levels, key=levels.get), "loudest mic"
    return None, "unclear: use the script order"


def save_sheets(sheet_rows, fps, sdir, rows=8):
    for p in range(0, len(sheet_rows), rows):
        part = sheet_rows[p:p + rows]
        sheet = Image.new("RGB", (3 * 320, len(part) * 180))
        for r, (t, row) in enumerate(part):
            for c, k in enumerate(FEEDS):
                if k not in row:
                    continue
                im = Image.fromarray(row[k])
                d = ImageDraw.Draw(im)
                label = f"{k} {tc(t / fps)} f{t}"
                d.rectangle([0, 0, 7 * len(label) + 4, 13], fill="black")
                d.text((2, 1), label, fill="yellow")
                sheet.paste(im, (c * 320, r * 180))
        sheet.save(os.path.join(sdir, f"cams_{p // rows:02d}.jpg"), quality=85)


def load_recording(path, rid):
    with open(path) as f:
        data = json.load(f)
    rec = next((r for r in data["recordings"] if r["id"] == rid), None)
    if not rec or rec["kind"] != "multicam":
        raise SystemExit(f"{rid} isn't a multicam recording in {path}")
    cams = {f["role"]: {"path": f["path"], "offsetFrames": int(f.get("offsetFrames", 0))}
            for f in rec["files"] if f["role"] in FEEDS}
    if "M" not in cams:
        raise SystemExit(f"recording {rid} has no M (two-shot) camera file")
    return data.get("fps", "60000/1001"), rec, cams


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    show_dir, rec_path, rid = argv
    rate, rec, cams = load_recording(rec_path, rid)
    fps = fps_value(rate)
    adir = os.path.join(show_dir, "analysis", rid)
    os.makedirs(os.path.join(adir, "sheets"), exist_ok=True)
    n = common_length(cams, fps)
    if rec.get("frames"):
        n = min(n, int(rec["frames"]))
    print(f"recording {rid}: {n} frames ({n / fps:.1f} s), cameras {', '.join(cams)}", flush=True)
    package = os.environ.get("INFOCUS_PACKAGE")
    st, sheet_rows = scan(cams, n, fps, package)
    feeds = feed_runs(st, fps)
    save_sheets(sheet_rows, fps, os.path.join(adir, "sheets"))

    src = cams["M"]
    wav = os.path.join(adir, "audio16k.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{int(src['offsetFrames']) / fps:.6f}", "-i", src["path"],
                    "-t", f"{n / fps:.6f}", "-vn", "-ac", "1", "-ar", "16000", wav], check=True)
    runs = speech_runs(wav)
    segs = transcribe(wav, adir, n / fps)
    with open(os.path.join(adir, "transcript.txt"), "w") as f:
        for a, b, w in segs:
            f.write(f"{a:8.2f} {b:8.2f}  f{int(a * fps):6d}  {w}\n")

    speech = []
    for a_s, b_s in runs:
        a, b = int(a_s * fps), min(n, int(b_s * fps))
        if b - a < fps * 0.5:
            continue
        mouths = mouth_scores(cams, a, b, fps)
        loud = feed_loudness(cams, a, b, fps)
        side, why = reader(mouths, loud)
        said = " ".join(w for s0, s1, w in segs if s0 < b_s and s1 > a_s)
        speech.append({"start": a, "end": b, "said": said, "mouth": mouths, "loudness": loud, "reader": side,
                       "why": why})
    result = {"recording": rid, "fps": rate, "frames": n, "cameras": cams, "feeds": feeds, "speech": speech,
              "transcript": segs}
    with open(os.path.join(adir, "multicam.json"), "w") as f:
        json.dump(result, f, indent=1)

    lines = [f"# Multicam recording {rid} ({n} frames, {n / fps:.1f} s)", "",
             "Frame numbers are on this recording's timeline (frame t = file frame t + offsetFrames): use them as "
             f"edl.json \"in\"/\"out\" with \"recording\": \"{rid}\".", "", "## Feeds", ""]
    for k, info in feeds.items():
        lines.append(f"- **{k}** ({os.path.basename(cams[k]['path'])}, offset {cams[k]['offsetFrames']}): shows the "
                     f"{k} {'two-shot' if k == 'M' else 'set'} on {info['shows_own_set']:.0%} of frames; "
                     f"frozen (held) stretches: {len(info['frozen'])}")
        odd = [r for r in info["runs"] if r["cam"] != k]
        for r in odd[:8]:
            lines.append(f"  - f{r['start']}–f{r['end'] - 1}: looks like {r['cam']}")
    lines += ["", "## Speech (who is reading)", "",
              "| start | end | reader | why | mouth movement | feed loudness dB | said |", "|---|---|---|---|---|---|---|"]
    for s in speech:
        mouth = ", ".join(f"{k} {v}" for k, v in s["mouth"].items())
        loud = ", ".join(f"{k} {v}" for k, v in s["loudness"].items())
        lines.append(f"| {tc(s['start'] / fps)} f{s['start']} | {tc(s['end'] / fps)} f{s['end']} | "
                     f"{s['reader'] or '?'} | {s['why']} | {mouth} | {loud} | {s['said'][:200]} |")
    lines += ["", f"Contact sheets (M | L | R): `{os.path.join(adir, 'sheets')}` · transcript: "
                  f"`{os.path.join(adir, 'transcript.txt')}`"]
    with open(os.path.join(adir, "multicam.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"done → {os.path.join(adir, 'multicam.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
