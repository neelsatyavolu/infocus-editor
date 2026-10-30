"""Re-composite every source take onto the show's clean virtual set, with the anchors matted and re-seated.

For each take: measure the anchors on a few frames (Vision faces and mattes), work out which set the recording was
filmed on, and fit one scale + offset that seats them (seat.py). Then render every frame in parallel worker
processes: the theme's set, the fresh card (or the TV standby) on the TV, the anchors moved into their seats, and the
table's front panel in front. build_ffmpeg.py reads the result instead of the recording, the card and the foreground.

    uv run --with numpy --with opencv-python python recomposite.py <show_dir> [--only <take id>] [--workers N]

Writes Recomposite/<id> - Recomposite.mov per take, its before/after/alpha stills, and Recomposite/plan.json:
{"theme", "seconds", "abandoned", "flag", "takes": [{"id", "recording", "cam", "recorded_theme", "s", "tx", "ty",
"status": "recomposited"|"fallback", "flag", "note", "tight"}]}. A take that fails its checks keeps its original
picture when that is the same set the show goes out on; otherwise it keeps its best matte and is flagged, so the
show never mixes two sets.
"""
import json
import math
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anchor_alpha import (CARD_INK, POSE_MIN, alpha_for, arm_mask, behind_table, blend, cut_and_measure,  # noqa: E402
                          matte_kind, patch_mask, roi, steady, texture, unmix)
from common import (H, SCREENS, W, card_file, ensure_tool, fps_value, is_fixup, load_edl,  # noqa: E402
                    overlay_file, package_dir, recomposite_file, recording_of, seek_args, segment_source,
                    take_signature, theme_file, theme_name)
from seat import (LIP_PANEL, SCALE_RANGE, SHARE_PX, SHARE_SCALE, edge_y, edges_touched, fit,  # noqa: E402
                  settle, share_transforms)
from virtual_set import SET_KEY, geometry, recorded_theme, set_image  # noqa: E402

SAMPLES = 6
BUDGET_S = 1800                               # the whole pass; past it a same-set show keeps its recorded picture
MIN_FACE_SHARE = 0.5                          # of the sampled frames, where every anchor's face must be found
MATTE_JUMP, MATTE_JUMP_SHARE = 0.25, 0.05     # a frame whose matte area is 25% off the take's median is a jump
CLEAN_SHARE = 0.25                            # sample around old graphics only if this much of the take is free of them
MAX_EMPTY_SHARE = 0.10                        # frames with no anchor at all (a switcher flash) are held, up to this
COLOR = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]


def chunks(n, workers):
    """[(first, count)] covering frames [0, n) once, in at most `workers` parts."""
    k = max(1, min(workers, n))
    base, extra = divmod(n, k)
    out, first = [], 0
    for i in range(k):
        count = base + (1 if i < extra else 0)
        out.append((first, count))
        first += count
    return out


def on_failure(recorded, output):
    """What a failed take shows: its original picture only when that is the set the show goes out on."""
    return "fallback" if recorded == output else "keep"


def take_ok(found, samples, areas, wanted, known_set=True):
    """(ok, reason): filmed on a set the package has, every anchor's face on at least half the sampled frames, a
    scale the seat can reach, and a matte that doesn't jump."""
    if not known_set:
        return False, "it was filmed on a set the package doesn't have, so the matte can't use it"
    if found < samples * MIN_FACE_SHARE:
        return False, f"the anchors' faces were found on only {found} of {samples} sampled frames"
    if wanted is not None and not SCALE_RANGE[0] <= wanted <= SCALE_RANGE[1]:
        return False, f"seating them needs a scale of {wanted:.2f}, outside {SCALE_RANGE[0]}-{SCALE_RANGE[1]}"
    if areas:
        a = np.asarray(areas, np.float32)
        empty = float(np.mean(a == 0))
        if empty > MAX_EMPTY_SHARE:
            return False, f"there's no anchor on {empty:.0%} of frames"
        a = a[a > 0]                                                 # held frames (a flash) aren't jumps
        med = float(np.median(a))
        jumps = float(np.mean(np.abs(a - med) / med > MATTE_JUMP))
        if jumps > MATTE_JUMP_SHARE:
            return False, f"the matte jumps on {jumps:.0%} of frames"
    return True, ""


def hold_map(areas):
    """For every frame, the frame whose picture it shows: a frame with no anchor (the switcher flashed a full-screen
    card) holds the nearest earlier frame with one, or at the start of the take the first later one."""
    good = [i for i, a in enumerate(areas) if a > 0]
    if not good:
        return list(range(len(areas)))
    out, last = [], None
    for i, a in enumerate(areas):
        if a > 0:
            last = i
        out.append(i if a > 0 else (last if last is not None else good[0]))
    return out


def apply_holds(clip, mapping, rate):
    """Re-encode a clip so frame i shows frame mapping[i]."""
    targets = {m for i, m in enumerate(mapping) if m != i}
    def frames():
        p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", clip, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             stdout=subprocess.PIPE)
        while (b := p.stdout.read(W * H * 3)) and len(b) == W * H * 3:
            yield b
        p.wait()
    kept = {i: b for i, b in enumerate(frames()) if i in targets}
    tmp = clip + ".held.mov"
    enc = subprocess.Popen(encode_args(rate, tmp), stdin=subprocess.PIPE)
    for i, b in enumerate(frames()):
        enc.stdin.write(kept[mapping[i]] if mapping[i] != i else b)
    enc.stdin.close()
    if enc.wait():
        raise RuntimeError(f"couldn't re-encode {clip}")
    os.replace(tmp, clip)


def encode_args(rate, out):
    return ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", rate, "-i",
            "-", "-vf", "scale=out_color_matrix=bt709:out_range=tv", "-c:v", "libx264", "-crf", "12", "-preset", "fast",
            "-pix_fmt", "yuv420p", *COLOR, out]


def over_budget(elapsed_s, frames_done, frames_total, budget_s=BUDGET_S):
    """Whether the pass, at the speed so far, would run past its budget (never once everything is rendered)."""
    return 0 < frames_done < frames_total and elapsed_s / frames_done * frames_total > budget_s


def reader_args(path, first, count, fps, pix_fmt, vf=""):
    """ffmpeg args that write `count` raw frames of `path` from frame `first` exactly, by index. The seek lands a
    second early and trim picks the frame; the timestamps are reset and passed through, or the raw output fills the
    trimmed-off second by repeating its first frame."""
    pre, k = seek_args(first, fps)
    chain = f"trim=start_frame={k},setpts=PTS-STARTPTS" + (f",{vf}" if vf else "")
    return ["ffmpeg", "-v", "error", *pre, "-i", path, "-vf", chain, "-frames:v", str(count), "-fps_mode",
            "passthrough", "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]


def read_frame(path, frame, fps):
    pre, k = seek_args(frame, fps)
    out = subprocess.run(["ffmpeg", "-v", "error", *pre, "-i", path, "-vf", f"select=eq(n\\,{k})", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    if len(out) != W * H * 3:
        raise RuntimeError(f"frame {frame} is past the end of {path}")
    return np.frombuffer(out, np.uint8).reshape(H, W, 3)


def vision_mattes(path, first, count, keep):
    """Generator of Vision instance mattes (float 0..1) for file frames [first, first + count)."""
    p = subprocess.Popen([ensure_tool("anchor_matte"), path, str(first), str(count), str(keep)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            b = p.stdout.read(W * H)
            if len(b) < W * H:
                break
            yield np.frombuffer(b, np.uint8).reshape(H, W).astype(np.float32) / 255
    finally:
        p.kill()
        p.wait()


def face_boxes(images):
    out = subprocess.run([ensure_tool("face_boxes"), *images], capture_output=True, text=True, check=True)
    return [json.loads(line)["faces"] for line in out.stdout.splitlines()]


def per_frame_cuts(window, at, boxes, clip_rate, show_rate):
    """[(t, t, box)] for each recording frame t of an old graphic's window: the box its fresh cover (placed at
    recording frame `at`, drawn-pixel `boxes` per clip frame) has drawn on that frame. Old and fresh graphics animate
    in step (the cover goes on 2 frames early), so while the cover is still small only that much is cut."""
    out = []
    for t in range(window["first"], window["last"] + 1):
        i = math.floor((t - at) * clip_rate / show_rate)
        if 0 <= i < len(boxes) and boxes[i]:
            out.append((t, t, tuple(boxes[i])))
    return out


def patch_runs(cuts, take_in, take_out):
    """[(first, last, clean frame)] for each run of consecutive frames with an old graphic over the anchor: the
    nearest frame of the take without one (just before the run, else just after it), or None when the graphic
    covers the whole take."""
    frames = sorted({t for a, b, _ in cuts for t in range(a, b + 1) if take_in <= t < take_out})
    runs = []
    for t in frames:
        if runs and t == runs[-1][1] + 1:
            runs[-1][1] = t
        else:
            runs.append([t, t])
    out = []
    for a, b in runs:
        ref = a - 1 if a - 1 >= take_in else (b + 1 if b + 1 < take_out else None)
        out.append((a, b, ref))
    return out


def overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def cut_windows(baked, overlays, seg, show_dir, cover_boxes=None, show_rate=60000 / 1001):
    """Old burned-in graphics inside a take: [(first, last, box)] in recording frames, filled from a clean frame of
    the take so they never travel with the anchor. Under a fresh cover (one on screen at the same time, drawn over
    the same zone) the box follows the cover frame by frame: `cover_boxes(overlay)` -> (boxes, clip rate). Otherwise
    it is the old graphic's whole zone."""
    from verify_render import ZONES
    fresh = [o for o in overlays if o.get("covers") and not is_fixup(o, show_dir)]
    drawn = {o["id"]: cover_boxes(o) if cover_boxes else None for o in fresh}
    out = []
    for w in baked:
        if w["zone"] not in ZONES or w["first"] >= seg["out"] or w["last"] < seg["in"]:
            continue
        zone = ZONES[w["zone"]]
        cover = next((o for o in fresh if o["covers"][0] <= w["last"] and o["covers"][1] >= w["first"]
                      and drawn[o["id"]] and any(b and overlaps(b, zone) for b in drawn[o["id"]][0])), None)
        if cover:
            boxes, clip_rate = drawn[cover["id"]]
            out += per_frame_cuts(w, cover["at"], [b if b and overlaps(b, zone) else None for b in boxes],
                                  clip_rate, show_rate)
        else:
            out.append((w["first"], w["last"], zone))
    return out


def old_graphic_boxes(show_dir, edl, seg):
    """cut_windows for a program take, from its recording's analysis and its covers' rendered clips."""
    rec = recording_of(edl, seg)
    if rec["kind"] != "program":
        return []
    from verify_render import alpha_boxes, load_analysis
    try:
        analysis = load_analysis(show_dir, edl, rec["id"])
    except FileNotFoundError:
        return []

    def cover_boxes(o):
        path = overlay_file(show_dir, o, edl)
        if not os.path.exists(path):
            return None
        boxes, clip_rate, _ = alpha_boxes(path)
        return boxes, clip_rate
    overlays = [o for o in edl.get("overlays", []) if recording_of(edl, o)["id"] == rec["id"]]
    return cut_windows(analysis.get("baked_overlays", []), overlays, seg, show_dir, cover_boxes,
                       fps_value(edl["fps"]))


def arm_poses(path, first, count):
    """Vision body pose of file frames [first, first + count): one list of people per frame (see arm_poses.swift)."""
    out = subprocess.run([ensure_tool("arm_poses"), path, str(first), str(count)], capture_output=True, text=True,
                         check=True).stdout
    return [json.loads(line) for line in out.splitlines()]


def elbows(poses, boxes, keep):
    """Each anchor's [(x, elbow row, lowest wrist row or None, (left, right) shoulder x or None)] over the sampled
    frames, matched to their face by the shoulders; None unless every anchor's elbows were seen on at least half
    the samples."""
    per = [sorted(b, key=lambda r: -r[2])[:keep] for b in boxes if len(b) >= keep]
    if not per:
        return None
    face_x = sorted(float(np.median([sorted(b, key=lambda r: r[0])[i][0] + sorted(b, key=lambda r: r[0])[i][2] / 2
                                     for b in per])) for i in range(keep))
    rows, wrists, spans = [[] for _ in face_x], [[] for _ in face_x], [[] for _ in face_x]
    for people in poses:
        for p in people:
            js = [p[k] for k in ("ls", "rs") if k in p and p[k][2] >= POSE_MIN]
            es = [p[k][1] for k in ("le", "re") if k in p and p[k][2] >= POSE_MIN]
            ws = [p[k][1] for k in ("lw", "rw") if k in p and p[k][2] >= POSE_MIN]
            if not js or not es:
                continue
            mx = float(np.mean([j[0] for j in js]))
            i = int(np.argmin([abs(mx - x) for x in face_x]))
            if abs(mx - face_x[i]) < 250:
                rows[i].append(float(np.mean(es)))
                if len(js) == 2:
                    spans[i].append(sorted(j[0] for j in js))
                if ws:
                    wrists[i].append(float(max(ws)))
    if any(len(r) < len(poses) / 2 for r in rows):
        return None
    return [(x, float(np.median(r)), float(np.median(w)) if len(w) >= len(poses) / 2 else None,
             tuple(float(v) for v in np.median(sp, 0)) if len(sp) >= len(poses) / 2 else None)
            for x, r, w, sp in zip(face_x, rows, wrists, spans)]


def usable(mats):
    """Indices of the sampled frames with someone in them (not a switcher flash); all of them if none has."""
    keep = [i for i, m in enumerate(mats) if (m > 0.5).mean() >= 0.01]
    return keep or list(range(len(mats)))


def sample_frames(take_in, take_out, avoid, n):
    """n recording frames spread over a take, from the frames with no old graphic on screen (`avoid`: [(first,
    last)]) when they're a fair share of it, so a burned-in name bar's edge is never measured as the anchor's."""
    clean = [t for t in range(take_in, take_out) if not any(a <= t <= b for a, b in avoid)]
    pool = clean if len(clean) >= max(n, CLEAN_SHARE * (take_out - take_in)) else list(range(take_in, take_out))
    return [pool[int(len(pool) * (i + 0.5) / n)] for i in range(n)]


def measure(edl, seg, theme, pkg, tmp, show_dir=""):
    """Where a take's anchors are and the transform that seats them."""
    path, offset = segment_source(edl, seg)
    fps = fps_value(edl["fps"])
    cam = seg["cam"]
    keep = 2 if cam == "M" else 1
    frames, mats, pngs = [], [], []
    picks = sample_frames(seg["in"], seg["out"], [(a, b) for a, b, _ in old_graphic_boxes(show_dir, edl, seg)],
                          SAMPLES)
    for i, t in enumerate(picks):
        f = t + offset
        frames.append(read_frame(path, f, fps))
        mats.append(next(vision_mattes(path, f, 1, keep)))
        pngs.append(os.path.join(tmp, f"{seg['id']}-{i}.png"))
        cv2.imwrite(pngs[-1], cv2.cvtColor(frames[-1], cv2.COLOR_RGB2BGR))
    idx = usable(mats)
    frames, mats, pngs = [frames[i] for i in idx], [mats[i] for i in idx], [pngs[i] for i in idx]
    picks = [picks[i] for i in idx]
    boxes = face_boxes(pngs)
    masks = [cv2.dilate((m > 0.25).astype(np.uint8), np.ones((31, 31), np.uint8)).astype(bool) for m in mats]
    recorded = recorded_theme(frames, masks, cam, pkg)
    plate = set_image(cam, recorded, pkg) if recorded is not None else None
    found_boxes = [r for b in boxes for r in b]
    chin = float(np.median([r[1] + r[3] for r in found_boxes])) if found_boxes else H / 2
    alphas = [alpha_for(f, vis, plate, cam, chin) for f, vis in zip(frames, mats)]
    touch = edges_touched([np.maximum(vis, a) for vis, a in zip(mats, alphas)])
    geom = geometry(cam, theme)
    poses = [arm_poses(path, t + offset, 1)[0] for t in picks]
    s, tx, ty, note = fit(geom, boxes, touch, alphas, elbows(poses, boxes, keep))
    heights = [r[3] for b in boxes if len(b) >= keep for r in sorted(b, key=lambda r: -r[2])[:keep]]
    wanted = geom["seats"]["h"] / float(np.median(heights)) if heights else None
    return {"id": seg["id"], "recording": recording_of(edl, seg)["id"], "cam": cam, "recorded_theme": recorded,
            "s": s, "tx": tx, "ty": ty, "note": note, "tight": bool(touch["bottom"]), "chin": chin,
            "found": sum(len(b) >= keep for b in boxes), "samples": len(idx), "wanted": wanted}


def render_chunk(job):
    """Worker: composite file frames [first, first + count) of one take into a chunk file. Reads one frame either
    side so the matte's time median has neighbours. Returns the anchor area of every frame written."""
    cv2.setNumThreads(2)
    path, fps, first, count, cam = job["path"], job["fps"], job["first"], job["count"], job["cam"]
    lo = max(0, first - 1)
    lead, total = first - lo, count + (first - lo) + 1
    known = job["recorded"] is not None
    plate = set_image(cam, job["recorded"], job["pkg"]) if known else None
    tex = texture(plate) if known else None
    out_u8 = set_image(cam, job["theme"], job["pkg"])
    out_set = out_u8.astype(np.float32)
    behind = plate.copy() if known else None
    if known and cam in SCREENS:                                     # the switcher keyed the anchor over a card
        x, y, w, h = SCREENS[cam]
        behind[y:y + h, x:x + w] = CARD_INK
    s, tx, ty = job["s"], job["tx"], job["ty"]
    X, Y = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    mx, my = (X - tx) / s, (Y - ty) / s
    rows = np.arange(H, dtype=np.float32)[:, None]
    panel = edge_y(job["desk_lip"], np.arange(W, dtype=np.float32))[None, :] + LIP_PANEL
    table = cv2.GaussianBlur(np.clip(rows - panel + 1, 0, 1), (0, 0), 1)[..., None]

    raw = subprocess.Popen(reader_args(path, lo, total, fps, "rgb24"), stdout=subprocess.PIPE)
    pose = subprocess.Popen([ensure_tool("arm_poses"), path, str(lo), str(total)], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    back = np.asarray(job["desk_back"], np.float32)
    card = tv = None
    if cam in SCREENS:
        x, y, w, h = SCREENS[cam]
        if job["card"]:
            card = subprocess.Popen(reader_args(job["card"], job["card_first"], count, fps, "rgba",
                                                f"scale={w}:{h}:flags=lanczos"), stdout=subprocess.PIPE)
        else:
            standby = cv2.cvtColor(cv2.imread(job["standby"]), cv2.COLOR_BGR2RGB)
            tv = np.concatenate([cv2.resize(standby, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32),
                                 np.full((h, w, 1), 255, np.float32)], 2)
    enc = subprocess.Popen(encode_args(job["rate"], job["out"]), stdin=subprocess.PIPE)
    pending, areas = [], []
    cleans = {}

    def clean_frame(ref):
        if ref not in cleans:
            cleans[ref] = (read_frame(path, ref, fps), next(vision_mattes(path, ref, 1, job["keep"])))
        return cleans[ref]

    def mattes():
        for vis in vision_mattes(path, lo, total, job["keep"]):
            b = raw.stdout.read(W * H * 3)
            if len(b) < W * H * 3:
                return
            line = pose.stdout.readline()
            pending.append((np.frombuffer(b, np.uint8).reshape(H, W, 3), json.loads(line) if line.strip() else []))
            yield vis

    for i, vis in enumerate(steady(mattes())):
        f, people = pending.pop(0)
        if i < lead:
            continue
        if len(areas) == count:
            break
        t = first + len(areas)                                       # file frame of this output
        comp = out_u8.copy()
        if cam in SCREENS:
            x, y, w, h = SCREENS[cam]
            if card is not None:
                cb = card.stdout.read(w * h * 4)
                if len(cb) == w * h * 4:
                    tv = np.frombuffer(cb, np.uint8).reshape(h, w, 4).astype(np.float32)
            if tv is not None:                                      # past the card's end it holds its last frame
                ca = tv[..., 3:] / 255
                comp[y:y + h, x:x + w] = np.clip(tv[..., :3] * ca + out_set[y:y + h, x:x + w] * (1 - ca), 0, 255)
        olds = [b for a0, a1, b in job["boxes"] if a0 <= t <= a1]
        ref = next((r for a0, a1, r in job["runs"] if a0 <= t <= a1), None)
        if olds and ref is not None:                                 # the old graphic's area from a clean frame
            rf, rv = clean_frame(ref)
            m = patch_mask(f, rf, olds, only_graphic=True)
            f = np.clip(blend(f, rf, m), 0, 255).astype(np.uint8)
            vis = blend(vis, rv, m)
            olds = []
        a_full, area = None, 0
        box = roi(vis)
        if box is not None:
            # All the work happens in the box around the anchor, then in where that box lands once moved.
            y0, y1, x0, x1 = box
            crop = (slice(y0, y1), slice(x0, x1))
            a = alpha_for(f[crop], vis[crop], plate[crop] if known else None, cam, job["chin"], origin=(y0, x0),
                          tex=tex[crop] if known else None)
            a, area = cut_and_measure(a, [(bx0 - x0, by0 - y0, bx1 - x0, by1 - y0) for bx0, by0, bx1, by1 in olds])
            # The table top goes in front: under its back edge (where it lands once moved) only arms and hands stay.
            xs = np.arange(x0, x1, dtype=np.float32)
            edge_rows = (edge_y(back, s * xs + tx) - ty) / s - y0
            a = behind_table(a, arm_mask(people, a.shape, (y0, x0)) if people else None, edge_rows)
            fg = (unmix(f[crop], a, behind[crop]) if known else f[crop].astype(np.float32)) * a[..., None]
            oy0, oy1 = max(0, int(s * y0 + ty) - 2), min(H, int(np.ceil(s * y1 + ty)) + 2)
            ox0, ox1 = max(0, int(s * x0 + tx) - 2), min(W, int(np.ceil(s * x1 + tx)) + 2)
            if oy0 < oy1 and ox0 < ox1:
                out = (slice(oy0, oy1), slice(ox0, ox1))
                kx, ky = mx[out] - np.float32(x0), my[out] - np.float32(y0)
                prem = cv2.remap(fg, kx, ky, cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT)
                a2 = np.clip(cv2.remap(a, kx, ky, cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT), 0, 1)[..., None]
                c = prem + comp[out].astype(np.float32) * (1 - a2)
                tb = table[out]
                comp[out] = np.clip(c * (1 - tb) + out_set[out] * tb, 0, 255).astype(np.uint8)
            if t == job["still"]:
                a_full = np.zeros((H, W), np.float32)
                a_full[crop] = a
        enc.stdin.write(comp.tobytes())
        areas.append(area)
        if t == job["still"]:
            stem = job["stills"]
            cv2.imwrite(stem + "-before.jpg", cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
            cv2.imwrite(stem + "-after.jpg", cv2.cvtColor(comp, cv2.COLOR_RGB2BGR))
            alpha = a_full if a_full is not None else np.zeros((H, W), np.float32)
            cv2.imwrite(stem + "-alpha.jpg", (alpha * 255).astype(np.uint8))
    enc.stdin.close()
    enc.wait()
    for p in (raw, card, pose):
        if p is not None:
            p.kill()
            p.wait()
    if len(areas) != count:
        raise RuntimeError(f"{path}: got {len(areas)} of {count} frames from {first}")
    return areas


def take_jobs(edl, show_dir, seg, entry, theme, pkg, workers, tmp):
    """The render_chunk jobs that render one take, split across `workers`."""
    path, offset = segment_source(edl, seg)
    start, n = seg["in"] + offset, seg["out"] - seg["in"]
    cam = seg["cam"]
    card = card_file(show_dir, seg) if cam in SCREENS and seg.get("card") else None
    if card and not os.path.exists(card):
        raise RuntimeError(f"take {seg['id']}: no card at {card}; run render_graphics.mjs first")
    out = recomposite_file(show_dir, seg)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    boxes = [(a + offset, b + offset, box) for a, b, box in old_graphic_boxes(show_dir, edl, seg)]
    runs = patch_runs(boxes, start, start + n)
    jobs = []
    for i, (first, count) in enumerate(chunks(n, workers)):
        jobs.append({"path": path, "fps": fps_value(edl["fps"]), "rate": edl["fps"], "first": start + first,
                     "count": count, "cam": cam, "keep": 2 if cam == "M" else 1, "recorded": entry["recorded_theme"],
                     "theme": theme, "pkg": pkg, "s": entry["s"], "tx": entry["tx"], "ty": entry["ty"],
                     "chin": entry["chin"], "desk_lip": geometry(cam, theme)["desk_lip"],
                     "desk_back": geometry(cam, theme)["desk_back"], "card": card,
                     "card_first": first, "standby": theme_file("standby", edl, pkg), "boxes": boxes, "runs": runs,
                     "still": start + n // 2, "stills": os.path.join(os.path.dirname(out), seg["id"]),
                     "out": os.path.join(tmp, f"{seg['id']}-{i:03d}.mp4")})
    return jobs


def render_take(pool, edl, show_dir, seg, entry, theme, pkg, workers, tmp):
    """Render one take's clip; returns its per-frame anchor areas."""
    jobs = take_jobs(edl, show_dir, seg, entry, theme, pkg, workers, tmp)
    out = recomposite_file(show_dir, seg)
    areas = [a for part in pool.map(render_chunk, jobs) for a in part]
    listing = os.path.join(tmp, f"{seg['id']}-chunks.txt")
    with open(listing, "w") as f:
        f.writelines(f"file '{j['out']}'\n" for j in jobs)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", out],
                   check=True)
    mapping = hold_map(areas)
    if mapping != list(range(len(areas))):
        apply_holds(out, mapping, edl["fps"])
    return areas


def apply_seats(entries, by_id):
    """The editing agent's own seat for a take (edl.json "seat": {"scale", "x", "y"}) replaces the script's guess.
    Returns new dicts."""
    out = []
    for e in entries:
        seat = by_id.get(e["id"], {}).get("seat")
        scale = float(seat.get("scale", 1.0)) if seat else None
        out.append({**e, "s": scale, "tx": float(seat.get("x", 0)), "ty": float(seat.get("y", 0)),
                    "wanted": scale, "seated_by": "agent"} if seat else dict(e))
    return out


def preview(edl, show_dir, seg, entry, theme, pkg, tmp):
    """One frame from the middle of a take, re-composited with `entry`'s seat: <id>-preview-after.jpg, and the same
    with the table's back edge (magenta) and front lip (yellow) drawn on, <id>-preview-guide.jpg."""
    job = take_jobs(edl, show_dir, seg, entry, theme, pkg, 1, tmp)[0]
    middle = job["still"]
    stem = os.path.join(os.path.dirname(recomposite_file(show_dir, seg)), f"{seg['id']}-preview")
    render_chunk({**job, "first": middle, "count": 1, "card_first": middle - job["first"], "stills": stem,
                  "out": os.path.join(tmp, "preview.mp4")})
    img = cv2.imread(stem + "-after.jpg")
    geom = geometry(seg["cam"], theme)
    cv2.polylines(img, [np.array(geom["desk_back"], np.int32)], False, (255, 0, 255), 2)
    cv2.polylines(img, [np.array(geom["desk_lip"], np.int32)], False, (0, 255, 255), 2)
    cv2.imwrite(stem + "-guide.jpg", img)
    return stem + "-after.jpg", stem + "-guide.jpg"


def adopt(entry, siblings):
    """A take re-rendered on its own (--only) takes its camera's transform from the takes already rendered, unless
    it measured clearly differently (the camera moved)."""
    if not siblings:
        return dict(entry)
    med = {k: float(np.median([t[k] for t in siblings])) for k in ("s", "tx", "ty")}
    close = abs(entry["s"] - med["s"]) <= SHARE_SCALE and all(abs(entry[k] - med[k]) <= SHARE_PX for k in ("tx", "ty"))
    return {**entry, **med} if close else dict(entry)


def plan_entry(e, show_dir, edl, seg):
    """A take's line in plan.json, with what it was made from (stale_takes compares it before every build)."""
    keys = ("id", "recording", "cam", "recorded_theme", "s", "tx", "ty", "status", "flag", "note", "tight")
    return {**{k: e[k] for k in keys}, "seated_by": e.get("seated_by", "script"),
            "source": take_signature(show_dir, edl, seg)}


def flag(seg_id, reason, action):
    what = "kept its original picture" if action == "fallback" else "kept its best matte on the new set: check it"
    return f"Take '{seg_id}' couldn't be re-composited cleanly ({reason}); {what}."


def write_plan(show_dir, plan):
    path = os.path.join(show_dir, "Recomposite", "plan.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(plan, f, indent=1)
    os.replace(path + ".tmp", path)


def check_inputs(show_dir, edl, takes, theme, pkg):
    """Fail before anything renders: the theme's sets, every side take's card, a recording kind we can matte."""
    for cam in SET_KEY:
        set_image(cam, theme, pkg)
    for seg in takes:
        matte_kind(recording_of(edl, seg))
        if seg.get("cam") in SCREENS and seg.get("card") and not os.path.exists(card_file(show_dir, seg)):
            raise SystemExit(f"take {seg['id']}: no card at {card_file(show_dir, seg)}; run render_graphics.mjs first")


def seat_takes(entries, old, only):
    """One transform per camera: takes that measured about the same share it, a nearly seated camera is left
    alone, and a take re-rendered on its own adopts the transform its camera's other takes were rendered with."""
    seated = settle(share_transforms([e for e in entries if e["found"]]))
    entries = [next((x for x in seated if x["id"] == e["id"]), e) for e in entries]
    if only:
        def siblings(e):
            return [t for t in old.get("takes", []) if t["id"] != e["id"] and t["status"] == "recomposited"
                    and (t["recording"], t["cam"]) == (e["recording"], e["cam"])]
        entries = [adopt(e, siblings(e)) if e["found"] else e for e in entries]
    return entries


def render_all(edl, show_dir, takes, entries, theme, pkg, workers, tmp):
    """Render every take that can be re-composited; returns (plan flag, abandoned reason)."""
    by_id = {s["id"]: s for s in takes}
    for e in entries:
        e["known"] = e["recorded_theme"] is not None
        e["ok"], e["reason"] = take_ok(e["found"], e["samples"], [], e["wanted"], e["known"])
        e["action"] = on_failure(e["recorded_theme"], theme)
        skip = not e["ok"] and e["action"] == "fallback"
        e.update(status="fallback" if skip else "recomposited",
                 flag=flag(e["id"], e["reason"], e["action"]) if skip else None)
    todo = [e for e in entries if e["status"] == "recomposited"]
    total = sum(by_id[e["id"]]["out"] - by_id[e["id"]]["in"] for e in todo)
    done, render_s, plan_flag = 0, 0.0, None
    with multiprocessing.get_context("spawn").Pool(workers) as pool:
        for e in todo:
            seg = by_id[e["id"]]
            t0 = time.time()
            areas = render_take(pool, edl, show_dir, seg, e, theme, pkg, workers, tmp)
            render_s += time.time() - t0
            done += seg["out"] - seg["in"]
            ok, reason = take_ok(e["found"], e["samples"], areas, e["wanted"], e["known"])
            if not e["ok"] or not ok:
                e["flag"] = flag(e["id"], e["reason"] or reason, e["action"])
                if e["action"] == "fallback":
                    e["status"] = "fallback"
                    os.remove(recomposite_file(show_dir, seg))
            filmed = "an unknown" if e["recorded_theme"] is None else e["recorded_theme"] or "regular"
            print(f"{e['id']} [{e['cam']}, filmed on {filmed} set]: scale {e['s']:.3f}, "
                  f"offset ({e['tx']:.0f}, {e['ty']:.0f}) -> {e['status']}{' (flagged)' if e['flag'] else ''}",
                  flush=True)
            if plan_flag is None and over_budget(render_s, done, total):
                minutes = render_s / done * total / 60
                if all(x["recorded_theme"] == theme for x in entries):
                    return None, (f"Re-compositing would take about {minutes:.0f} min, over the {BUDGET_S // 60} min "
                                  "budget; the show keeps its recorded picture.")
                plan_flag = (f"Re-compositing is running slow (about {minutes:.0f} min); it carried on because the "
                             "show goes out on a different set.")
    return plan_flag, None


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    show_dir = argv[1]
    look = argv[argv.index("--preview") + 1] if "--preview" in argv else None
    only = look or (argv[argv.index("--only") + 1] if "--only" in argv else None)
    workers = int(argv[argv.index("--workers") + 1]) if "--workers" in argv else max(2, (os.cpu_count() or 4) // 2)
    edl = load_edl(show_dir)
    theme, pkg = theme_name(edl), package_dir(edl)
    takes = [s for s in edl["segments"] if s["kind"] == "source" and (only is None or s["id"] == only)]
    if not takes:
        raise SystemExit(f"no source take{' ' + only if only else ''} in edl.json")
    check_inputs(show_dir, edl, takes, theme, pkg)
    started = time.time()
    plan_path = os.path.join(show_dir, "Recomposite", "plan.json")
    old = {"takes": []}
    if only and os.path.exists(plan_path):
        with open(plan_path) as f:
            old = json.load(f)
    tmp = tempfile.mkdtemp(prefix="recomposite-")
    try:
        entries = seat_takes([measure(edl, seg, theme, pkg, tmp, show_dir) for seg in takes], old, only)
        entries = apply_seats(entries, {s["id"]: s for s in takes})
        if look:
            e = entries[0]
            after, guide = preview(edl, show_dir, takes[0], e, theme, pkg, tmp)
            print(f"{e['id']}: seat scale {e['s']:.3f}, x {e['tx']:.0f}, y {e['ty']:.0f} "
                  f"({e.get('seated_by', 'script')}); filmed on {e['recorded_theme']!r} set\n{after}\n{guide}")
            return 0
        plan_flag, abandoned = render_all(edl, show_dir, takes, entries, theme, pkg, workers, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    by_id = {s["id"]: s for s in takes}
    if abandoned:
        for e in entries:
            e.update(status="fallback", flag=None)
            clip = recomposite_file(show_dir, by_id[e["id"]])
            if os.path.exists(clip):
                os.remove(clip)
    keep_old = [t for t in old.get("takes", []) if t["id"] not in by_id]
    plan = {"theme": theme, "seconds": round(time.time() - started), "abandoned": abandoned,
            "flag": plan_flag or old.get("flag"),
            "takes": keep_old + [plan_entry(e, show_dir, edl, by_id[e["id"]]) for e in entries]}
    write_plan(show_dir, plan)
    print(f"wrote {plan_path} in {plan['seconds']} s" + (f"; ABANDONED: {abandoned}" if abandoned else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
