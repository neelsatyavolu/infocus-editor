"""First look at an InFocus show recording: what's in it and where.

    uv run --with numpy --with opencv-python --with pillow --with soundfile python analyze.py <recording.mov> <show_dir>
The package folder comes from $INFOCUS_PACKAGE (or --package <dir>). --out <dir> writes somewhere other than
<show_dir>/analysis (one folder per recording when a show has several: analysis/<recording id>).

Writes <show_dir>/analysis/:
  report.md       shot list (camera per shot + what's said), baked-in overlays, filmed TV cards, restart cues
  analysis.json   the same, machine-readable (frame numbers are in the recording's own frame rate)
  sheets/         contact sheets, one frame every 4 s, labelled time / frame / camera
  cards/          a still of every filmed TV card and full-screen card, to read the on-air text from
  transcript.txt  whisper transcript with absolute times
Cameras: M = middle (two-shot), L / R = side sets with the TV, FS = full-screen card, BLACK, OTHER (intro, outro, packages).
"""
import json
import os
import re
import subprocess
import sys

import cv2
import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (FOLLOW_BOX, NAMEBAR_BAND, SCREENS, THEMES, WHISPER_MODEL, fps_value, package_dir, probe,  # noqa: E402
                    theme_name)

SW, SH = 480, 270                      # analysis resolution (1080p / 4)
Q = 4
SHEET_EVERY_S = 4.0
RESTART_CUES = re.compile(r"start (over|again|from)|from the top|wait|sorry|hold on|are they recording|can i|"
                          r"who'?s on (the )?teleprompter|one more time|let me redo|messed up", re.I)


def box(b):
    x0, y0, x1, y1 = b
    return slice(y0 // Q, y1 // Q), slice(x0 // Q, x1 // Q)


def rect(r):
    x, y, w, h = r
    return slice(y // Q, (y + h) // Q), slice(x // Q, (x + w) // Q)


def tc(sec):
    return f"{int(sec // 60)}:{sec % 60:05.2f}"


def green_mask(f):
    f = f.astype(np.int16)
    return (f[..., 1] > f[..., 0] + 40) & (f[..., 1] > f[..., 2] + 20) & (f[..., 1] > 90)


def pumpkin_mask(f):
    """The Halloween accent: Pumpkin #FF7A1A and the Deep Pumpkin #B4440A role strip (skin tones stay out)."""
    f = f.astype(np.int16)
    return (f[..., 0] > f[..., 1] + 80) & (f[..., 1] > f[..., 2] + 30) & (f[..., 0] > 150)


def brand_mask(f, theme=""):
    """Pixels in the package overlays' accent color. A Halloween show may still carry a regular green graphic."""
    return green_mask(f) | pumpkin_mask(f) if theme == "halloween" else green_mask(f)


def middle_set(package=None):
    """The empty Middle set(s) at analysis resolution: what an M two-shot looks like behind the anchors. One per
    installed theme (stacked), so a two-shot on a seasonal set is still an M shot whatever the brief says."""
    pkg = package or package_dir()
    sets = []
    for theme in THEMES.values():
        png = os.path.join(pkg, theme["middle"])
        if os.path.exists(png):
            mid = cv2.cvtColor(cv2.imread(png), cv2.COLOR_BGR2RGB)
            sets.append(cv2.resize(mid, (SW, SH), interpolation=cv2.INTER_AREA).astype(np.int16))
    if not sets:
        raise RuntimeError(f"No Middle set in the package ({os.path.join(pkg, THEMES['']['middle'])})")
    return np.stack(sets)


SCREEN_SLICES = {k: rect(v) for k, v in SCREENS.items()}


def classify(f, mid):
    """Camera class of one analysis-resolution RGB frame: M, L, R, FS, BLACK or OTHER."""
    mx = f.max(2)
    dark = mx < 45
    if f.mean() < 8:
        return "BLACK"
    mids = mid if mid.ndim == 4 else mid[None]
    if min(np.median(np.abs(f.astype(np.int16) - m).max(2)) for m in mids) < 18:
        return "M"
    sl = SCREEN_SLICES
    dl, dr, dall = dark[sl["L"]].mean(), dark[sl["R"]].mean(), dark.mean()
    if dall > 0.8:
        return "FS"
    if dl > 0.75 and dl > dr + 0.15:
        return "L"
    if dr > 0.75 and dr > dl + 0.15:
        return "R"
    return "OTHER"


def classify_frames(path, every_s=2.0, package=None):
    """Camera class (M, L, R, FS, BLACK, OTHER) of one frame every `every_s` seconds of a video, in order: a quick
    picture profile of a file (the worker uses it to tell program feeds from camera feeds)."""
    mid = middle_set(package)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps=1/{every_s},scale={SW}:{SH}:flags=area",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    out = []
    while True:
        buf = p.stdout.read(SW * SH * 3)
        if len(buf) < SW * SH * 3:
            break
        out.append(classify(np.frombuffer(buf, np.uint8).reshape(SH, SW, 3), mid))
    p.stdout.close()
    if p.wait():
        raise RuntimeError(f"ffmpeg couldn't read {path}")
    return out


def scan_video(path, fps, n_frames, package=None):
    mid = middle_set(package)
    theme = theme_name()
    sl = SCREEN_SLICES
    lb = box((NAMEBAR_BAND[0], NAMEBAR_BAND[1], 960, NAMEBAR_BAND[3]))
    rb = box((960, NAMEBAR_BAND[1], NAMEBAR_BAND[2], NAMEBAR_BAND[3]))
    fb = box(FOLLOW_BOX)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={SW}:{SH}:flags=area",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    st = {k: [] for k in ("diff", "cam", "o_left", "o_right", "o_follow", "text_L", "text_R")}
    sheet_frames, prev, i = [], None, 0
    every = int(round(SHEET_EVERY_S * fps))
    while True:
        buf = p.stdout.read(SW * SH * 3)
        if len(buf) < SW * SH * 3:
            break
        f = np.frombuffer(buf, np.uint8).reshape(SH, SW, 3)
        small = cv2.resize(f, (96, 54), interpolation=cv2.INTER_AREA).astype(np.int16)
        st["diff"].append(float(np.abs(small - prev).mean()) if prev is not None else 0.0)
        prev = small
        mx = f.max(2)
        cam = classify(f, mid)
        st["cam"].append(cam)
        o = brand_mask(f, theme)                 # package overlays carry the accent (strip, icon ring, Follow body)
        st["o_left"].append(int(o[lb].sum()))
        st["o_right"].append(int(o[rb].sum()))
        st["o_follow"].append(int(o[fb].sum()))
        for k in ("L", "R"):                    # card text = bright pixels on the screen away from the standby icon
            s = mx[sl[k]] > 150
            h, w = s.shape
            s[int(h * 0.3):int(h * 0.7), int(w * 0.38):int(w * 0.62)] = False
            st[f"text_{k}"].append(float(s.mean()) if cam == k else 0.0)
        if i % every == 0:
            sheet_frames.append((i, cam, cv2.resize(f, (320, 180), interpolation=cv2.INTER_AREA)))
        i += 1
        if i % 3000 == 0:
            print(f"  scanned {i}/{n_frames} frames", flush=True)
    return {k: np.array(v) for k, v in st.items()}, sheet_frames


def shots_from(st, fps):
    d = st["diff"]
    cuts = [0]
    for k in range(1, len(d)):
        local = np.median(d[max(0, k - 15):k + 15])
        if d[k] > max(12.0, 6 * local) and k - cuts[-1] > 3:
            cuts.append(k)
    cuts.append(len(d))
    shots = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        vals, counts = np.unique(st["cam"][a:b], return_counts=True)
        cam = str(vals[np.argmax(counts)])
        if shots and shots[-1]["cam"] == cam:   # same camera both sides (intro collage, flashes): one shot
            shots[-1]["end"] = int(b)
        else:
            shots.append({"start": int(a), "end": int(b), "cam": cam})
    for s in shots:
        s["t0"], s["t1"] = round(s["start"] / fps, 3), round(s["end"] / fps, 3)
    return shots


def windows(mask, min_len, max_gap=3):
    """Runs of True, merging gaps up to max_gap frames."""
    idx = np.where(mask)[0]
    runs = []
    for k in idx:
        if runs and k - runs[-1][1] <= max_gap + 1:
            runs[-1][1] = k
        else:
            runs.append([k, k])
    return [(int(a), int(b)) for a, b in runs if b - a + 1 >= min_len]


def overlay_windows(st, fps):
    """Old package overlays burned into studio shots. The baseline is per camera (a low percentile of that
    camera's frames), so set greenery and dark furniture cancel out but a long overlay doesn't hide itself."""
    out = []
    cams = st["cam"]
    zones = (("name bar (left)", "o_left", ("M", "L", "R")), ("name bar (right)", "o_right", ("M", "L", "R")),
             ("Follow card", "o_follow", ("M", "R")))   # the Left set's TV sits where the Follow card goes
    for zone, key, ok_cams in zones:
        g = st[key].astype(float)
        lift = np.zeros_like(g)
        for c in ok_cams:
            sel = cams == c
            if sel.any():
                lift[sel] = g[sel] - np.percentile(g[sel], 20)
        seen = set()
        for a, b in windows(lift > 12, min_len=int(fps * 1.0)):
            while a > 0 and lift[a - 1] > 1:
                a -= 1
            while b + 1 < len(lift) and lift[b + 1] > 1:
                b += 1
            if (a, b) in seen:
                continue
            seen.add((a, b))
            out.append({"zone": zone, "first": a, "last": b, "t0": round(a / fps, 2), "t1": round(b / fps, 2),
                        "cams": sorted(set(cams[a:b + 1]))})
    return sorted(out, key=lambda o: o["first"])


def card_windows(st, fps):
    out = []
    for k in ("L", "R"):
        for a, b in windows(st[f"text_{k}"] > 0.006, min_len=int(fps * 0.5), max_gap=int(fps * 0.3)):
            out.append({"cam": k, "first": a, "last": b, "t0": round(a / fps, 2), "t1": round(b / fps, 2)})
    return sorted(out, key=lambda o: o["first"])


def grab(path, frame, fps, out, crop=None):
    vf = ["scale=1920:1080:flags=lanczos"]
    if crop:
        x, y, w, h = crop
        vf.append(f"crop={w}:{h}:{x}:{y}")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{(frame + 0.25) / fps:.4f}", "-i", path, "-frames:v", "1",
                    "-vf", ",".join(vf), out], check=True)


def audio(path, adir):
    info = probe(path)
    ch = next(s["channels"] for s in info["streams"] if s["codec_type"] == "audio")
    live = []
    for c in range(ch):
        r = subprocess.run(["ffmpeg", "-nostats", "-i", path, "-af", f"pan=mono|c0=c{c},volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
        m = re.search(r"mean_volume: (-?[\d.]+)", r)
        if m and float(m.group(1)) > -70:
            live.append(c)
    mix = "+".join(f"{1 / len(live):.3f}*c{c}" for c in live)
    wav = os.path.join(adir, "audio16k.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-af", f"pan=mono|c0={mix}", "-ar", "16000", wav], check=True)
    return wav, live


def speech_runs(wav):
    x, sr = sf.read(wav)
    hop = sr // 20
    e = np.array([np.sqrt(np.mean(x[i:i + hop] ** 2)) for i in range(0, len(x) - hop, hop)])
    db = 20 * np.log10(e + 1e-9)
    thr = np.percentile(db, 10) + 14
    runs = []
    for a, b in windows(db > thr, min_len=3, max_gap=6):
        runs.append((round(a / 20, 2), round((b + 1) / 20, 2)))
    return runs


def transcribe(wav, adir, total_s, chunk=120, overlap=4):
    segs = []
    t = 0.0
    while t < total_s:
        part = os.path.join(adir, "chunk.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-t", str(chunk + overlap), "-i", wav, part], check=True)
        stem = os.path.join(adir, "chunk")
        subprocess.run(["whisper-cli", "-m", WHISPER_MODEL, "-f", part, "-l", "en", "-ml", "60", "-sow", "-osrt",
                        "-of", stem, "-np"], capture_output=True)
        txt = open(stem + ".srt").read() if os.path.exists(stem + ".srt") else ""
        for a, b, w in re.findall(r"(\d\d:\d\d:\d\d,\d+) --> (\d\d:\d\d:\d\d,\d+)\n(.*)", txt):
            s0, s1 = (t + sum(float(v) * m for v, m in zip(x.replace(",", ".").split(":"), (3600, 60, 1))) for x in (a, b))
            if t > 0 and s0 < t + overlap / 2:
                continue                        # already covered by the previous chunk
            if t + chunk + overlap < total_s and s0 >= t + chunk + overlap / 2:
                continue
            w = w.strip()
            if w and not (segs and segs[-1][2] == w and segs[-2:-1] and segs[-2][2] == w):   # drop looping hallucinations
                segs.append((round(s0, 2), round(s1, 2), w))
        t += chunk
    for f in ("chunk.wav", "chunk.srt"):
        if os.path.exists(os.path.join(adir, f)):
            os.remove(os.path.join(adir, f))
    return segs


def save_sheets(sheet_frames, fps, sdir, cols=6, rows=5):
    per = cols * rows
    for p in range(0, len(sheet_frames), per):
        sheet = Image.new("RGB", (cols * 320, rows * 180))
        for i, (fi, cam, img) in enumerate(sheet_frames[p:p + per]):
            im = Image.fromarray(img)
            d = ImageDraw.Draw(im)
            label = f"{tc(fi / fps)} f{fi} {cam}"
            d.rectangle([0, 0, 7 * len(label) + 4, 13], fill="black")
            d.text((2, 1), label, fill="yellow")
            sheet.paste(im, ((i % cols) * 320, (i // cols) * 180))
        sheet.save(os.path.join(sdir, f"sheet_{p // per:02d}.jpg"), quality=85)


def main():
    args = sys.argv[1:]
    if "--package" in args:
        k = args.index("--package")
        os.environ["INFOCUS_PACKAGE"] = args[k + 1]
        del args[k:k + 2]
    adir = None
    if "--out" in args:
        k = args.index("--out")
        adir = args[k + 1]
        del args[k:k + 2]
    path, show_dir = args[0], args[1]
    adir = adir or os.path.join(show_dir, "analysis")
    for sub in ("sheets", "cards"):
        os.makedirs(os.path.join(adir, sub), exist_ok=True)
    info = probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    fps = fps_value(v["r_frame_rate"])
    dur = float(info["format"]["duration"])
    n = int(v.get("nb_frames") or round(dur * fps))
    print(f"{path}: {v['width']}x{v['height']} @ {fps:.3f}, {dur:.1f} s, {n} frames", flush=True)

    print("scanning picture…", flush=True)
    st, sheet_frames = scan_video(path, fps, n)
    shots = shots_from(st, fps)
    ovs = overlay_windows(st, fps)
    cards = card_windows(st, fps)
    save_sheets(sheet_frames, fps, os.path.join(adir, "sheets"))
    for c in cards:
        c["still"] = os.path.join(adir, "cards", f"{c['cam']}_f{c['first']}.png")
        grab(path, (c["first"] + c["last"]) // 2, fps, c["still"], crop=SCREENS[c["cam"]])
    for s in shots:
        if s["cam"] == "FS" and s["end"] - s["start"] > 5:
            s["still"] = os.path.join(adir, "cards", f"FS_f{s['start']}.png")
            grab(path, (s["start"] + s["end"]) // 2, fps, s["still"])

    print("audio + transcript…", flush=True)
    wav, live = audio(path, adir)
    runs = speech_runs(wav)
    segs = transcribe(wav, adir, dur)
    with open(os.path.join(adir, "transcript.txt"), "w") as f:
        for a, b, w in segs:
            f.write(f"{a:8.2f} {b:8.2f}  f{int(a * fps):6d}  {w}\n")

    for s in shots:
        s["said"] = " ".join(w for a, b, w in segs if a < s["t1"] and b > s["t0"])
        s["restart_cue"] = bool(RESTART_CUES.search(s["said"]))
    result = {"recording": path, "fps": v["r_frame_rate"], "frames": n, "duration": dur, "size": [v["width"], v["height"]],
              "audio_channels_with_signal": live, "shots": shots, "baked_overlays": ovs, "filmed_cards": cards,
              "speech_runs_s": runs, "transcript": segs}
    with open(os.path.join(adir, "analysis.json"), "w") as f:
        json.dump(result, f, indent=1)

    lines = [f"# Analysis: {os.path.basename(path)}", "",
             f"{v['width']}x{v['height']} at {v['r_frame_rate']} ({fps:.3f} fps), {dur:.1f} s, {n} frames. "
             f"Audio channels with signal: {live}. Frame numbers are in this file's frame rate.", "",
             ]
    lines += ["## Shots", "", "| # | start | end | len | cam | said |", "|---|---|---|---|---|---|"]
    for k, s in enumerate(shots):
        cue = " ⚠️ restart?" if s["restart_cue"] else ""
        lines.append(f"| {k} | {tc(s['t0'])} f{s['start']} | {tc(s['t1'])} f{s['end']} | {s['t1'] - s['t0']:.1f}s | "
                     f"{s['cam']} | {s['said'][:300]}{cue} |")
    lines += ["", "## Baked-in overlays (old graphics to cover)", "",
              "First/last visible frame (±1; confirm with sheet.py before placing a cover).", ""]
    lines += [f"- {o['zone']}: f{o['first']}–f{o['last']} ({tc(o['t0'])}–{tc(o['t1'])}) on {','.join(o['cams'])}" for o in ovs] or ["- none found"]
    lines += ["", "## Filmed TV cards (read the text from the stills)", ""]
    lines += [f"- {c['cam']} TV: f{c['first']}–f{c['last']} ({tc(c['t0'])}–{tc(c['t1'])}) → `{c['still']}`" for c in cards] or ["- none found"]
    fs = [s for s in shots if s.get("still")]
    if fs:
        lines += ["", "## Full-screen cards", ""] + [f"- f{s['start']}–f{s['end']} → `{s['still']}`" for s in fs]
    lines += ["", f"Contact sheets: `{os.path.join(adir, 'sheets')}` · transcript: `{os.path.join(adir, 'transcript.txt')}`"]
    with open(os.path.join(adir, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"done → {os.path.join(adir, 'report.md')}")


if __name__ == "__main__":
    main()
