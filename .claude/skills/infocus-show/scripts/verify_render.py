"""Hard checks on a rendered show. The edit is finished only when this passes; the worker runs it itself.

    uv run --with numpy --with opencv-python --with pillow --with soundfile \\
      python verify_render.py <show_dir> [--render <file>] [--only 1,6]
Reads edl.json, layout.json, Audio/levels.json (if present) and, for each program recording, its analyze.py output
(analysis/<recording id>/analysis.json; analysis/analysis.json for a show with only "source"). Takes from a
multicam recording have no switcher graphics or switcher intro to check, so rules 2-4 and the switcher part of rule
5 apply to program recordings only, and rules 9-10 to multicam recordings only.
Writes <show_dir>/check/verify.json: {"pass", "failures": [{"rule", "ruleNumber", "segment"|"overlay", "frames", "detail"}],
"loudness": [...]}. Exit 0 = pass, 1 = fail, 2 = could not run.

Rules (frames are render frames for 1, source frames for 2-4):
  1 switcher_frames  every frame of a source segment in the render is a studio shot (M, L or R), never a full-screen
                     card, black or anything else (catches a one-frame card flash and a hold made from the wrong frame)
  2 cover_span       every frame of each old graphic a cover claims (covers: [first, last]) has the cover on screen
                     with something drawn (catches a fresh dual third that goes out before the old one)
  3 cover_shape      while both are fully up, the old graphic's box sits inside the cover's drawn box (+2 px)
  4 uncovered        every old graphic analyze.py found inside a chosen take has a cover; every side-camera
                     segment has a card
  5 intro_outro      the show opens with the package intro and ends with the package outro, and no take reaches
                     into the switcher's own intro or end card
  6 audio            every segment's true peak is -6.0 ±0.3 dBFS; anchor segments sit within 1 dB of the dialogue
                     loudness (plus whatever master_audio.py had to lift them to reach the peak)
  7 basics           video + audio, 1920x1080 at the edl rate, as many frames as layout.json says (±2)
  8 side_set         every L/R take shows its side set (TV detected) in the render and carries a fresh card
  9 shot_length      multicam: no shot (consecutive same-camera takes) is under 1.5 s
 10 av_sync          multicam: on every camera, the render's sound lines up with that feed's own sound within
                     1 frame (cross-checks the sync offsets in edl "cameras")
 11 recording_range  every take lies inside its recording (a take never spans two recordings)
"""
import json
import os
import subprocess
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import SH, SW, classify, green_mask, middle_set  # noqa: E402
from check_render import loudness_rows  # noqa: E402
from common import (FOLLOW_BOX, H, NAMEBAR_BAND, W, expand, fps_value, is_multicam, load_edl,  # noqa: E402
                    overlay_file, package_dir, probe, recording_of, recordings, seek_args, segment_source)

RULES = {1: "switcher_frames", 2: "cover_span", 3: "cover_shape", 4: "uncovered", 5: "intro_outro", 6: "audio",
         7: "basics", 8: "side_set", 9: "shot_length", 10: "av_sync", 11: "recording_range"}
MIN_SHOT_S = 1.5
SIDE_SHARE = 0.9     # rule 8: share of a take's frames that must show its side set
SYNC_SR = 8000       # rule 10 analysis rate
SYNC_SEARCH = 6      # rule 10: frames searched either way
SYNC_MIN_CONF = 4.0  # rule 10: correlation peak / mean, below which a window can't be judged
STUDIO = ("M", "L", "R")
INTRO_FILE, OUTRO_FILE = "InFocus Intro 2026.mp4", "InFocus Outro 2026.mp4"
ZONES = {"name bar (left)": (NAMEBAR_BAND[0], NAMEBAR_BAND[1], W // 2, NAMEBAR_BAND[3]),
         "name bar (right)": (W // 2, NAMEBAR_BAND[1], NAMEBAR_BAND[2], NAMEBAR_BAND[3]),
         "Follow card": FOLLOW_BOX}
ZONE_PAD = 30        # old plates can sit a little outside the analysis zone
BOX_PAD = 2          # rule 3 slack, px
EDGE_TOL = 2         # rule 4 slack on analysis windows (they're ±1 frame)


def fail(n, detail, **where):
    return {"rule": RULES[n], "ruleNumber": n, **where, "detail": detail}


def runs(frames):
    out = []
    for f in sorted(frames):
        if out and f == out[-1][1] + 1:
            out[-1][1] = f
        else:
            out.append([f, f])
    return out


def source_segments(edl):
    return [s for s in edl["segments"] if s["kind"] == "source"]


def recording_view(edl, rid):
    """edl.json narrowed to one program recording, shaped like a single-recording show ("source" = its program
    file), so the program-recording rules can run on it unchanged."""
    if not edl.get("recordings"):
        return edl
    rec = recordings(edl)[rid]
    view = {k: v for k, v in edl.items() if k != "recordings"}
    view["segments"] = [s for s in edl["segments"] if s["kind"] != "source" or recording_of(edl, s)["id"] == rid]
    view["overlays"] = [o for o in edl.get("overlays", []) if recording_of(edl, o)["id"] == rid]
    view["source"] = next(f["path"] for f in rec["files"] if f["role"] == "program")
    return view


def load_analysis(show_dir, edl, rid):
    path = os.path.join(show_dir, "analysis", rid, "analysis.json")
    if not os.path.exists(path) and not edl.get("recordings"):
        path = os.path.join(show_dir, "analysis", "analysis.json")
    with open(path) as f:
        return json.load(f)


def majority_cam(analysis, a, b):
    """Most common camera over source frames [a, b) according to analyze.py's shots."""
    counts = {}
    for s in analysis.get("shots", []):
        n = min(b, s["end"]) - max(a, s["start"])
        if n > 0:
            counts[s["cam"]] = counts.get(s["cam"], 0) + n
    return max(counts, key=counts.get) if counts else None


def src_to_rec(edl, layout):
    rec = {s["id"]: s["rec_in"] for s in layout["segments"]}
    spans = [(s["in"], s["out"], rec[s["id"]]) for s in source_segments(edl) if s["id"] in rec]

    def to_rec(f):
        for a, b, r in spans:
            if a <= f < b:
                return r + f - a
        return None
    return to_rec


# ---------- rule 1 ----------

def render_cams(render, edl, layout):
    """Camera class of every render frame inside a source take: {take id: [class, ...]} ("missing" past the end)."""
    segs = [s for s in layout["segments"] if s["kind"] == "source" and s["rec_out"] > s["rec_in"]]
    if not segs:
        return {}
    mid = middle_set(package_dir(edl))
    expr = "+".join(f"between(n\\,{s['rec_in']}\\,{s['rec_out'] - 1})" for s in segs)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", render, "-vf", f"select='{expr}',scale={SW}:{SH}:flags=area",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         stdout=subprocess.PIPE)
    size = SW * SH * 3
    cams = {}
    for s in segs:
        seen = []
        for _ in range(s["rec_in"], s["rec_out"]):
            buf = p.stdout.read(size)
            seen.append(classify(np.frombuffer(buf, np.uint8).reshape(SH, SW, 3), mid) if len(buf) == size
                        else "missing")
        cams[s["id"]] = seen
    p.stdout.close()
    p.wait()
    return cams


def rule_switcher_frames(cams, layout):
    failures = []
    for s in layout["segments"]:
        bad = {}
        for t, cam in zip(range(s["rec_in"], s["rec_out"]), cams.get(s["id"], [])):
            if cam not in STUDIO:
                bad.setdefault(cam, []).append(t)
        for cam, frames in bad.items():
            what = {"FS": "a full-screen card", "BLACK": "black", "OTHER": "a non-studio picture",
                    "missing": "no picture (render too short)"}[cam]
            for a, b in runs(frames):
                n = b - a + 1
                failures.append(fail(1, f"render frame{'s' if n > 1 else ''} {a}{f'–{b}' if n > 1 else ''} "
                                        f"({n} frame{'s' if n > 1 else ''}) show{'' if n > 1 else 's'} {what} inside take '{s['id']}'",
                                     segment=s["id"], frames=[a, b]))
    return failures


def rule_side_set(cams, edl, layout):
    rec = {s["id"]: s for s in layout["segments"]}
    failures = []
    for seg in source_segments(edl):
        if seg.get("cam") not in ("L", "R") or seg["id"] not in rec:
            continue
        r = rec[seg["id"]]
        seen = cams.get(seg["id"], [])
        share = sum(c == seg["cam"] for c in seen) / max(1, len(seen))
        if seen and share < SIDE_SHARE:
            failures.append(fail(8, f"take '{seg['id']}' is planned on the {seg['cam']} set but only {share:.0%} of "
                                    f"its frames show that set with its TV", segment=seg["id"],
                                 frames=[r["rec_in"], r["rec_out"] - 1]))
        if not seg.get("card"):
            failures.append(fail(8, f"take '{seg['id']}' is on the {seg['cam']} set but has no fresh card",
                                 segment=seg["id"], frames=[r["rec_in"], r["rec_out"] - 1]))
    return failures


def shots(edl):
    """Consecutive source takes on the same camera that continue each other's frames are one shot."""
    out = []
    prev = None
    for seg in edl["segments"]:
        if seg["kind"] != "source":
            prev = None
            continue
        if (prev and prev.get("cam") == seg.get("cam") and prev["out"] == seg["in"]
                and prev.get("recording") == seg.get("recording")):
            out[-1]["ids"].append(seg["id"])
            out[-1]["out"] = seg["out"]
        else:
            out.append({"ids": [seg["id"]], "cam": seg.get("cam"), "in": seg["in"], "out": seg["out"]})
        prev = seg
    return out


def rule_shot_length(edl):
    fps = fps_value(edl["fps"])
    multicam = {s["id"] for s in source_segments(edl) if is_multicam(edl, s)}
    return [fail(9, f"{sh['cam']} shot {'+'.join(sh['ids'])} lasts {(sh['out'] - sh['in']) / fps:.2f} s; shots "
                    f"must be at least {MIN_SHOT_S} s", segment=sh["ids"][0], frames=[sh["in"], sh["out"] - 1])
            for sh in shots(edl) if sh["ids"][0] in multicam and (sh["out"] - sh["in"]) / fps < MIN_SHOT_S - 1e-9]


def read_mono(path, start, seconds):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.6f}", "-t", f"{seconds:.6f}", "-i", path,
                          "-vn", "-ac", "1", "-ar", str(SYNC_SR), "-f", "f32le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.float32)


def sync_lag(a, b, max_lag):
    """Lag (samples) of b behind a by GCC-PHAT, searched within ±max_lag, and the peak's confidence."""
    n = min(len(a), len(b))
    if n < SYNC_SR:
        return None, 0.0
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    size = 1 << int(np.ceil(np.log2(2 * n)))
    spec = np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size))
    cc = np.fft.irfft(spec / (np.abs(spec) + 1e-12), size)
    cc = np.concatenate([cc[-max_lag:], cc[:max_lag + 1]])
    k = int(np.argmax(np.abs(cc)))
    conf = float(np.abs(cc[k]) / (np.abs(cc).mean() + 1e-12))
    return k - max_lag, conf


def rule_av_sync(render, edl, layout):
    """For each camera, compare the render's sound in that camera's takes with the feed's own sound at the same
    frames. A lag means the camera's offset and the audio's offset disagree."""
    fps = fps_value(edl["fps"])
    rec = {s["id"]: s for s in layout["segments"]}
    failures, notes = [], []
    by_cam = {}
    for seg in source_segments(edl):
        if is_multicam(edl, seg):
            by_cam.setdefault(f"{recording_of(edl, seg)['id']} {seg['cam']}", []).append(seg)
    for cam, segs in sorted(by_cam.items()):
        path, off = segment_source(edl, segs[0])
        if not any(st["codec_type"] == "audio" for st in probe(path)["streams"]):
            notes.append(f"{cam}: the feed has no sound, so its sync can't be cross-checked")
            continue
        lags = []
        for seg in sorted(segs, key=lambda x: x["out"] - x["in"], reverse=True)[:3]:
            n = min(seg["out"] - seg["in"], int(10 * fps))
            if n < 2 * fps or seg["id"] not in rec:
                continue
            a = read_mono(render, rec[seg["id"]]["rec_in"] / fps, n / fps)
            b = read_mono(path, (seg["in"] + off) / fps, n / fps)
            lag, conf = sync_lag(a, b, int(SYNC_SEARCH * SYNC_SR / fps))
            if lag is not None and conf >= SYNC_MIN_CONF:
                lags.append(lag / SYNC_SR * fps)
        if not lags:
            notes.append(f"{cam}: the sound match was too weak to cross-check its sync")
            continue
        lag = float(np.median(lags))
        if abs(lag) > 1.0 + 2 * fps / SYNC_SR:                # "within 1 frame", plus the measurement's step
            failures.append(fail(10, f"{cam} picture and sound are {abs(lag):.1f} frames apart (the render's sound is "
                                     f"{'late' if lag > 0 else 'early'} against the {cam} feed); re-check the sync "
                                     "offsets in edl.json \"recordings\"", segment=segs[0]["id"]))
    return failures, notes


def rule_recording_range(edl):
    failures = []
    for seg in source_segments(edl):
        rec = recording_of(edl, seg)
        end = rec.get("frames")
        if seg["in"] < 0 or seg["out"] <= seg["in"] or (end and seg["out"] > end):
            failures.append(fail(11, f"take '{seg['id']}' ({seg['in']}–{seg['out']}) isn't inside recording "
                                     f"{rec['id']} (0–{end or '?'}); a take can't run past its recording",
                                 segment=seg["id"], frames=[seg["in"], seg["out"] - 1]))
    return failures


# ---------- rules 2 and 3 ----------

def alpha_boxes(path):
    """Per frame of a cover clip: the bounding box (x0, y0, x1, y1) of its drawn pixels, or None; and whether every
    frame is a full-frame opaque picture."""
    info = probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    w, h = int(v["width"]), int(v["height"])
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                         stdout=subprocess.PIPE)
    boxes, opaque = [], True
    while True:
        buf = p.stdout.read(w * h * 4)
        if len(buf) < w * h * 4:
            break
        a = np.frombuffer(buf, np.uint8).reshape(h, w, 4)[..., 3]
        opaque = opaque and bool(a.min() > 240)
        ys, xs = np.nonzero(a > 16)
        boxes.append((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1) if len(xs) else None)
    p.wait()
    return boxes, fps_value(v["r_frame_rate"]), opaque


def read_frames(recording, first, last, box):
    """Source frames first..last (by index) cropped to box, as RGB arrays."""
    x0, y0, x1, y1 = (v - v % 2 for v in box)          # even: ffmpeg crops 4:2:0 video to even sizes
    pre, k = seek_args(first, fps_value(next(s for s in probe(recording)["streams"]
                                            if s["codec_type"] == "video")["r_frame_rate"]))
    n = last - first + 1
    p = subprocess.Popen(["ffmpeg", "-v", "error", *pre, "-i", recording,
                          "-vf", f"select=gte(n\\,{k}),crop={x1 - x0}:{y1 - y0}:{x0}:{y0}", "-frames:v", str(n),
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    size = (x1 - x0) * (y1 - y0) * 3
    frames = []
    for _ in range(n):
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        frames.append(np.frombuffer(buf, np.uint8).reshape(y1 - y0, x1 - x0, 3))
    p.stdout.close()
    p.wait()
    return frames


def graphic_look(frame, ref):
    """Pixels that differ from the clean reference and look like a package graphic (dark plate, brand green,
    white text), plus the brand-green ones among them."""
    changed = np.abs(frame.astype(np.int16) - ref.astype(np.int16)).max(2) > 40
    g = green_mask(frame)
    return changed & ((frame.max(2) < 45) | g | (frame.min(2) > 200)), changed & g


def old_graphic_region(crops, ref):
    """Where the old switcher graphic sits while it's up: graphic-looking pixels present in at least half the
    frames, grouped into blobs; only blobs carrying brand green count. Persistence drops anchors moving about, and
    the green test drops dark hair or shirts. Returns a mask of the crop, or None."""
    hits = np.zeros(ref.shape[:2], np.float32)
    green = np.zeros(ref.shape[:2], np.float32)
    for f in crops:
        look, g = graphic_look(f, ref)
        hits += look
        green += g
    steady = hits >= 0.5 * len(crops)
    steady_green = green >= 0.5 * len(crops)
    n, labels, _, _ = cv2.connectedComponentsWithStats(cv2.dilate(steady.astype(np.uint8), np.ones((5, 5), np.uint8)))
    keep = np.zeros_like(steady)
    for i in range(1, n):
        part = labels == i
        if (steady_green & part).sum() >= 30:
            keep |= part & cv2.dilate(steady.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    return keep if keep.any() else None


def box_of(mask, origin):
    """Bounding box of a graphic mask, ignoring rows and columns with only a few stray pixels (anti-aliasing, a
    strand of hair): a plate edge is a long run."""
    rows, cols = mask.sum(1), mask.sum(0)
    if not rows.any():
        return None
    ys = np.nonzero(rows >= max(8, 0.03 * rows.max()))[0]
    xs = np.nonzero(cols >= max(8, 0.03 * cols.max()))[0]
    if not len(xs) or not len(ys):
        return None
    ox, oy = origin
    return (int(xs.min()) + ox, int(ys.min()) + oy, int(xs.max()) + 1 + ox, int(ys.max()) + 1 + oy)


ANIMATION = 45       # frames at each end of an old graphic where it (and its cover) may still be animating


def zone_for(overlay, analysis):
    hits = [ZONES[w["zone"]] for w in analysis.get("baked_overlays", [])
            if w["zone"] in ZONES and w["first"] <= overlay["covers"][1] and w["last"] >= overlay["covers"][0]]
    if not hits:
        return None
    return (max(0, min(h[0] for h in hits) - ZONE_PAD), max(0, min(h[1] for h in hits) - ZONE_PAD),
            min(W, max(h[2] for h in hits) + ZONE_PAD), min(H, max(h[3] for h in hits) + ZONE_PAD))


def reference_frame(overlay, analysis, edl):
    """A clean frame of the same shot just before the old graphic (or just after it)."""
    first, last = overlay["covers"]
    for f in (first - 6, last + 6):
        shot = next((s for s in analysis.get("shots", []) if s["start"] <= f < s["end"]), None)
        in_shot = shot and shot["start"] <= first and last < shot["end"] + 7 and shot["start"] - 7 < last
        if shot and shot["cam"] in STUDIO and in_shot:
            return f
    return None


def rule_covers(edl, layout, analysis, show_dir):
    tl_fps = fps_value(edl["fps"])
    to_rec = src_to_rec(edl, layout)
    placed = {o["id"]: o for o in layout.get("overlays", [])}
    recording = expand(edl["source"], edl)
    failures = []
    for o in edl.get("overlays", []):
        if not o.get("covers"):
            continue
        p = placed.get(o["id"])
        if not p:
            failures.append(fail(2, f"overlay '{o['id']}' isn't on the timeline (layout.json)", overlay=o["id"],
                                 frames=o["covers"]))
            continue
        boxes, clip_fps, opaque = alpha_boxes(overlay_file(show_dir, o, edl))

        def cover_box(t):
            if not p["rec_in"] <= t < p["rec_out"]:
                return None
            k = int((t - p["rec_in"]) * clip_fps / tl_fps + 1e-6)
            return boxes[k] if k < len(boxes) else None

        frames = [(f, to_rec(f)) for f in range(o["covers"][0], o["covers"][1] + 1)]
        frames = [(f, t) for f, t in frames if t is not None]          # frames cut out of the show need no cover
        missing = [f for f, t in frames if cover_box(t) is None]
        for a, b in runs(missing):
            failures.append(fail(2, f"old graphic frames {a}–{b} ({b - a + 1}) have no cover on screen: the cover "
                                    f"'{o['id']}' {'ends' if a > o['covers'][0] else 'starts'} too "
                                    f"{'early' if a > o['covers'][0] else 'late'}", overlay=o["id"], frames=[a, b]))
        zone, ref = zone_for(o, analysis), reference_frame(o, analysis, edl)
        if opaque or zone is None or ref is None or not frames:
            continue
        first, last = frames[0][0], frames[-1][0]
        zone = tuple(v - v % 2 for v in zone)
        ref_crop = read_frames(recording, ref, ref, zone)
        crops = read_frames(recording, first, last, zone)
        region = old_graphic_region(crops, ref_crop[0]) if ref_crop and crops else None
        if region is None:
            continue
        by_frame = dict(zip([f for f, _ in frames], crops))
        steady = frames[ANIMATION:-ANIMATION] if len(frames) > 3 * ANIMATION else frames[len(frames) // 2:][:1]
        outside = []
        for f, t in steady:
            cb = cover_box(t)
            old = box_of(region & graphic_look(by_frame[f], ref_crop[0])[0], zone[:2]) if f in by_frame else None
            if cb and old and not (old[0] >= cb[0] - BOX_PAD and old[1] >= cb[1] - BOX_PAD and
                                   old[2] <= cb[2] + BOX_PAD and old[3] <= cb[3] + BOX_PAD):
                outside.append((f, old, cb))
        for a, b in runs([f for f, _, _ in outside]):
            old, cb = next((o_, c) for f, o_, c in outside if f == a)
            failures.append(fail(3, f"old graphic box {old} pokes out of cover '{o['id']}' box {cb} on frames "
                                    f"{a}–{b}", overlay=o["id"], frames=[a, b]))
    return failures


# ---------- rules 4 and 5 ----------

def rule_uncovered(edl, analysis):
    failures = []
    takes = source_segments(edl)
    covers = [o["covers"] for o in edl.get("overlays", []) if o.get("covers")]
    for w in analysis.get("baked_overlays", []):
        for s in takes:
            a, b = max(w["first"], s["in"]), min(w["last"], s["out"] - 1)
            if a > b:
                continue
            if not any(c[0] <= a + EDGE_TOL and c[1] >= b - EDGE_TOL for c in covers):
                failures.append(fail(4, f"old {w['zone']} on frames {a}–{b} in take '{s['id']}' has no cover",
                                     segment=s["id"], frames=[a, b]))
    for s in takes:
        cam = s.get("cam") or majority_cam(analysis, s["in"], s["out"])
        if cam in ("L", "R") and not s.get("card"):
            failures.append(fail(4, f"take '{s['id']}' is on the {cam} set but has no fresh card, so the filmed card "
                                    "shows", segment=s["id"], frames=[s["in"], s["out"] - 1]))
    return failures


def switcher_regions(analysis):
    """Frames before the first studio shot (the switcher's intro), and from the switcher's end card on: the last
    full-screen/other shot over 1 s in the final 40% of the recording."""
    fps = fps_value(analysis.get("fps", "60000/1001"))
    shots = analysis.get("shots", [])
    regions = []
    first_studio = next((s["start"] for s in shots if s["cam"] in STUDIO), None)
    if first_studio:
        regions.append(("the switcher's intro", 0, first_studio))
    total = analysis.get("frames") or (shots[-1]["end"] if shots else 0)
    ends = [s for s in shots if s["cam"] in ("FS", "OTHER") and s["end"] - s["start"] > fps
            and s["start"] >= 0.6 * total]
    if ends:
        regions.append(("the switcher's end card", ends[-1]["start"], total))
    return regions


def rule_switcher_regions(edl, analysis):
    """Program recording: no take reaches into the switcher's own intro or end card."""
    failures = []
    for name, a, b in switcher_regions(analysis):
        for s in source_segments(edl):
            if s["in"] < b and s["out"] > a:
                failures.append(fail(5, f"take '{s['id']}' reaches into {name} (frames {a}–{b - 1})",
                                     segment=s["id"], frames=[max(a, s["in"]), min(b, s["out"]) - 1]))
    return failures


def rule_intro_outro(edl, analysis=None):
    failures = []
    segs = edl["segments"]

    def is_file(seg, name):
        return seg and seg["kind"] == "file" and os.path.basename(seg.get("path", "")) == name
    if not segs or not is_file(segs[0], INTRO_FILE):
        failures.append(fail(5, f"the show must open with the package intro ({INTRO_FILE})",
                             segment=segs[0]["id"] if segs else None))
    if not segs or not is_file(segs[-1], OUTRO_FILE):
        failures.append(fail(5, f"the show must end with the package outro ({OUTRO_FILE})",
                             segment=segs[-1]["id"] if segs else None))
    if analysis is not None:
        failures += rule_switcher_regions(edl, analysis)
    return failures


# ---------- rules 6 and 7 ----------

MAX_DIALOGUE_SPREAD_DB = 3.0


def rule_audio(render, layout, edl, levels):
    cfg = {"peak_dbfs": -6.0, "dialogue_lufs": -16.0, **edl.get("audio", {})}
    rows = loudness_rows(render, layout, edl)
    failures = []
    dialogue = [r for r in rows if r["kind"] == "source" and r["lufs"] is not None]
    for r in rows:
        if not r["ok"]:
            failures.append(fail(6, f"peak: true peak {r['peak']} dBFS, must be {cfg['peak_dbfs']:.1f} ±0.3",
                                 segment=r["segment"]))
        if r["kind"] == "source" and r["lufs"] is not None:
            lift = max(0.0, float(levels.get(r["segment"], {}).get("lift_db", 0.0)))
            # The limiter can pull a very dynamic read up to ~2 dB under the target.
            lo, hi = cfg["dialogue_lufs"] - 2, cfg["dialogue_lufs"] + 1 + lift
            if not lo <= r["lufs"] <= hi:
                r["ok"] = False
                failures.append(fail(6, f"loudness: {r['lufs']} LUFS, must be {lo:.1f} to {hi:.1f}",
                                     segment=r["segment"]))
    if len(dialogue) > 1:
        loud, quiet = max(dialogue, key=lambda r: r["lufs"]), min(dialogue, key=lambda r: r["lufs"])
        if loud["lufs"] - quiet["lufs"] > MAX_DIALOGUE_SPREAD_DB:
            failures.append(fail(6, f"loudness jumps {loud['lufs'] - quiet['lufs']:.1f} dB between anchor segments "
                                    f"({quiet['segment']} {quiet['lufs']}, {loud['segment']} {loud['lufs']} LUFS); "
                                    f"keep them within {MAX_DIALOGUE_SPREAD_DB:.0f} dB", segment=loud["segment"]))
    return failures, rows


def rule_basics(render, layout, edl):
    try:
        info = probe(render)
    except subprocess.CalledProcessError:
        return [fail(7, f"can't read the render {render}")]
    failures = []
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not any(s["codec_type"] == "audio" for s in info["streams"]):
        failures.append(fail(7, "the render has no audio"))
    if not v:
        return failures + [fail(7, "the render has no video")]
    fps = fps_value(edl["fps"])
    if (int(v["width"]), int(v["height"])) != (W, H):
        failures.append(fail(7, f"the render is {v['width']}x{v['height']}, not {W}x{H}"))
    if abs(fps_value(v["r_frame_rate"]) - fps) > 0.01:
        failures.append(fail(7, f"the render runs at {fps_value(v['r_frame_rate']):.3f} fps, not {fps:.3f}"))
    n = int(v.get("nb_frames") or round(float(info["format"]["duration"]) * fps))
    if abs(n - layout["frames"]) > 2:
        failures.append(fail(7, f"the render has {n} frames; the timeline has {layout['frames']}",
                             frames=[n, layout["frames"]]))
    return failures


def verify(show_dir, render=None, only=None):
    edl = load_edl(show_dir)
    with open(os.path.join(show_dir, "layout.json")) as f:
        layout = json.load(f)
    levels_path = os.path.join(show_dir, "Audio", "levels.json")
    levels = {}
    if os.path.exists(levels_path):
        with open(levels_path) as f:
            levels = {s["id"]: s for s in json.load(f)["segments"]}
    render = render or os.path.join(show_dir, f"{edl['show']} (preview).mp4")
    want = set(only or RULES)
    failures, rows, notes = [], [], []
    basics = rule_basics(render, layout, edl)
    if 7 in want:
        failures += basics
    has_video = not any("no video" in f["detail"] or "can't read" in f["detail"] for f in basics)
    has_audio = not any("no audio" in f["detail"] for f in basics)
    cams = render_cams(render, edl, layout) if has_video and want & {1, 8} else {}
    if 1 in want:
        failures += rule_switcher_frames(cams, layout)
    if 5 in want:
        failures += rule_intro_outro(edl)
    used = {recording_of(edl, s)["id"] for s in source_segments(edl)}
    for rid, rec in recordings(edl).items():
        if rec["kind"] != "program" or rid not in used:
            continue
        analysis, view = load_analysis(show_dir, edl, rid), recording_view(edl, rid)
        baked = analysis.get("baked_overlays") and any(o.get("covers") for o in view.get("overlays", []))
        if want & {2, 3} and baked:
            failures += [f for f in rule_covers(view, layout, analysis, show_dir) if f["ruleNumber"] in want]
        if 4 in want:
            failures += rule_uncovered(view, analysis)
        if 5 in want:
            failures += rule_switcher_regions(view, analysis)
    if 6 in want and has_audio:
        audio_fails, rows = rule_audio(render, layout, edl, levels)
        failures += audio_fails
    if 8 in want:
        failures += rule_side_set(cams, edl, layout)
    if 9 in want:
        failures += rule_shot_length(edl)
    if 10 in want and has_audio:
        sync_fails, notes = rule_av_sync(render, edl, layout)
        failures += sync_fails
    if 11 in want:
        failures += rule_recording_range(edl)
    failures.sort(key=lambda f: f["ruleNumber"])
    return {"pass": not failures, "render": render, "failures": failures, "loudness": rows, "notes": notes}


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    show_dir, render, only = argv[0], None, None
    if "--render" in argv:
        render = argv[argv.index("--render") + 1]
    if "--only" in argv:
        only = {int(n) for n in argv[argv.index("--only") + 1].split(",")}
    try:
        result = verify(show_dir, render, only)
    except (FileNotFoundError, KeyError, RuntimeError, json.JSONDecodeError) as e:
        print(f"verify_render: can't run: {e}", file=sys.stderr)
        return 2
    os.makedirs(os.path.join(show_dir, "check"), exist_ok=True)
    with open(os.path.join(show_dir, "check", "verify.json"), "w") as f:
        json.dump(result, f, indent=1)
    for fl in result["failures"]:
        print(f"FAIL rule {fl['ruleNumber']} {fl['rule']}: {fl['detail']}")
    print("PASS" if result["pass"] else f"{len(result['failures'])} failure(s) → {os.path.join(show_dir, 'check', 'verify.json')}")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
