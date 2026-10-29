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
from anchor_alpha import CARD_INK, alpha_for, cut_and_measure, matte_kind, roi, steady, texture, unmix  # noqa: E402
from common import (H, SCREENS, W, card_file, ensure_tool, fps_value, is_fixup, load_edl,  # noqa: E402
                    package_dir, recomposite_file, recording_of, seek_args, segment_source, theme_file, theme_name)
from seat import LIP_PANEL, SCALE_RANGE, edge_y, edges_touched, fit, share_transforms  # noqa: E402
from virtual_set import SET_KEY, geometry, recorded_theme, set_image  # noqa: E402

SAMPLES = 6
BUDGET_S = 1800                               # the whole pass; past it a same-set show keeps its recorded picture
MIN_FACE_SHARE = 0.5                          # of the sampled frames, where every anchor's face must be found
MATTE_JUMP, MATTE_JUMP_SHARE = 0.25, 0.05     # a frame whose matte area is 25% off the take's median is a jump
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


def take_ok(found, samples, areas, wanted):
    """(ok, reason): every anchor's face on at least half the sampled frames, a scale the seat can reach, and a
    matte that doesn't jump."""
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
    """Whether the pass, at the speed so far, would run past its budget."""
    return frames_done > 0 and elapsed_s / frames_done * frames_total > budget_s


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


def cut_windows(baked, overlays, seg, show_dir):
    """Old burned-in graphics inside a take that a fresh package graphic covers: [(first, last, box)] in recording
    frames. Only those are cut out of the anchor, so the fresh cover hides the hole; a logo bug the switcher fired
    over the set wall simply disappears with the old set."""
    from verify_render import ZONES
    fresh = [o["covers"] for o in overlays if o.get("covers") and not is_fixup(o, show_dir)]
    return [(w["first"], w["last"], ZONES[w["zone"]]) for w in baked
            if w["zone"] in ZONES and w["first"] < seg["out"] and w["last"] >= seg["in"]
            and any(a <= w["last"] and b >= w["first"] for a, b in fresh)]


def old_graphic_boxes(show_dir, edl, seg):
    """cut_windows for a program take, from its recording's analysis."""
    rec = recording_of(edl, seg)
    if rec["kind"] != "program":
        return []
    from verify_render import load_analysis
    try:
        analysis = load_analysis(show_dir, edl, rec["id"])
    except FileNotFoundError:
        return []
    overlays = [o for o in edl.get("overlays", []) if recording_of(edl, o)["id"] == rec["id"]]
    return cut_windows(analysis.get("baked_overlays", []), overlays, seg, show_dir)


def measure(edl, seg, theme, pkg, tmp):
    """Where a take's anchors are and the transform that seats them."""
    path, offset = segment_source(edl, seg)
    fps = fps_value(edl["fps"])
    cam, n = seg["cam"], seg["out"] - seg["in"]
    keep = 2 if cam == "M" else 1
    frames, mats, pngs = [], [], []
    for i in range(SAMPLES):
        f = seg["in"] + offset + int(n * (i + 0.5) / SAMPLES)
        frames.append(read_frame(path, f, fps))
        mats.append(next(vision_mattes(path, f, 1, keep)))
        pngs.append(os.path.join(tmp, f"{seg['id']}-{i}.png"))
        cv2.imwrite(pngs[-1], cv2.cvtColor(frames[-1], cv2.COLOR_RGB2BGR))
    boxes = face_boxes(pngs)
    mid = SAMPLES // 2
    mask = cv2.dilate((mats[mid] > 0.25).astype(np.uint8), np.ones((31, 31), np.uint8)).astype(bool)
    recorded = recorded_theme(frames[mid], mask, cam, pkg)
    plate = set_image(cam, recorded, pkg)
    found_boxes = [r for b in boxes for r in b]
    chin = float(np.median([r[1] + r[3] for r in found_boxes])) if found_boxes else H / 2
    alphas = [alpha_for(f, vis, plate, cam, chin) for f, vis in zip(frames, mats)]
    touch = edges_touched([np.maximum(vis, a) for vis, a in zip(mats, alphas)])
    geom = geometry(cam, theme)
    s, tx, ty, note = fit(geom, boxes, touch, alphas)
    heights = [r[3] for b in boxes if len(b) >= keep for r in sorted(b, key=lambda r: -r[2])[:keep]]
    wanted = geom["seats"]["h"] / float(np.median(heights)) if heights else None
    return {"id": seg["id"], "recording": recording_of(edl, seg)["id"], "cam": cam, "recorded_theme": recorded,
            "s": s, "tx": tx, "ty": ty, "note": note, "tight": bool(touch["bottom"]), "chin": chin,
            "found": sum(len(b) >= keep for b in boxes), "wanted": wanted}


def render_chunk(job):
    """Worker: composite file frames [first, first + count) of one take into a chunk file. Reads one frame either
    side so the matte's time median has neighbours. Returns the anchor area of every frame written."""
    cv2.setNumThreads(2)
    path, fps, first, count, cam = job["path"], job["fps"], job["first"], job["count"], job["cam"]
    lo = max(0, first - 1)
    lead, total = first - lo, count + (first - lo) + 1
    plate = set_image(cam, job["recorded"], job["pkg"])
    tex = texture(plate)
    out_u8 = set_image(cam, job["theme"], job["pkg"])
    out_set = out_u8.astype(np.float32)
    behind = plate.copy()
    if cam in SCREENS:                                               # the switcher keyed the anchor over a card
        x, y, w, h = SCREENS[cam]
        behind[y:y + h, x:x + w] = CARD_INK
    s, tx, ty = job["s"], job["tx"], job["ty"]
    X, Y = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32))
    mx, my = (X - tx) / s, (Y - ty) / s
    rows = np.arange(H, dtype=np.float32)[:, None]
    panel = edge_y(job["desk_lip"], np.arange(W, dtype=np.float32))[None, :] + LIP_PANEL
    table = cv2.GaussianBlur(np.clip(rows - panel + 1, 0, 1), (0, 0), 1)[..., None]

    raw = subprocess.Popen(reader_args(path, lo, total, fps, "rgb24"), stdout=subprocess.PIPE)
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

    def mattes():
        for vis in vision_mattes(path, lo, total, job["keep"]):
            b = raw.stdout.read(W * H * 3)
            if len(b) < W * H * 3:
                return
            pending.append(np.frombuffer(b, np.uint8).reshape(H, W, 3))
            yield vis

    for i, vis in enumerate(steady(mattes())):
        f = pending.pop(0)
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
        a_full, area = None, 0
        box = roi(vis)
        if box is not None:
            # All the work happens in the box around the anchor, then in where that box lands once moved.
            y0, y1, x0, x1 = box
            crop = (slice(y0, y1), slice(x0, x1))
            a = alpha_for(f[crop], vis[crop], plate[crop], cam, job["chin"], origin=(y0, x0), tex=tex[crop])
            olds = [(bx0 - x0, by0 - y0, bx1 - x0, by1 - y0)
                    for a0, a1, (bx0, by0, bx1, by1) in job["boxes"] if a0 <= t <= a1]
            a, area = cut_and_measure(a, olds)
            fg = unmix(f[crop], a, behind[crop]) * a[..., None]
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
            cv2.imwrite(stem + "-alpha.jpg", ((a_full if a_full is not None else np.zeros((H, W))) * 255).astype(np.uint8))
    enc.stdin.close()
    enc.wait()
    for p in (raw, card):
        if p is not None:
            p.kill()
            p.wait()
    if len(areas) != count:
        raise RuntimeError(f"{path}: got {len(areas)} of {count} frames from {first}")
    return areas


def render_take(pool, edl, show_dir, seg, entry, theme, pkg, workers, tmp):
    """Render one take's clip; returns its per-frame anchor areas."""
    path, offset = segment_source(edl, seg)
    start, n = seg["in"] + offset, seg["out"] - seg["in"]
    cam = seg["cam"]
    card = card_file(show_dir, seg) if cam in SCREENS and seg.get("card") else None
    if card and not os.path.exists(card):
        raise RuntimeError(f"take {seg['id']}: no card at {card}; run render_graphics.mjs first")
    out = recomposite_file(show_dir, seg)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    boxes = [(a + offset, b + offset, box) for a, b, box in old_graphic_boxes(show_dir, edl, seg)]
    jobs = []
    for i, (first, count) in enumerate(chunks(n, workers)):
        jobs.append({"path": path, "fps": fps_value(edl["fps"]), "rate": edl["fps"], "first": start + first,
                     "count": count, "cam": cam, "keep": 2 if cam == "M" else 1, "recorded": entry["recorded_theme"],
                     "theme": theme, "pkg": pkg, "s": entry["s"], "tx": entry["tx"], "ty": entry["ty"],
                     "chin": entry["chin"], "desk_lip": geometry(cam, theme)["desk_lip"], "card": card,
                     "card_first": first, "standby": theme_file("standby", edl, pkg), "boxes": boxes,
                     "still": start + n // 2, "stills": os.path.join(os.path.dirname(out), seg["id"]),
                     "out": os.path.join(tmp, f"{seg['id']}-{i:03d}.mp4")})
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


def flag(seg_id, reason, action):
    what = "kept its original picture" if action == "fallback" else "kept its best matte on the new set: check it"
    return f"Take '{seg_id}' couldn't be re-composited cleanly ({reason}); {what}."


def write_plan(show_dir, plan):
    path = os.path.join(show_dir, "Recomposite", "plan.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(plan, f, indent=1)
    os.replace(path + ".tmp", path)


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    show_dir = argv[1]
    only = argv[argv.index("--only") + 1] if "--only" in argv else None
    workers = int(argv[argv.index("--workers") + 1]) if "--workers" in argv else max(2, (os.cpu_count() or 4) // 2)
    edl = load_edl(show_dir)
    theme, pkg = theme_name(edl), package_dir(edl)
    for cam in SET_KEY:                                              # a missing set fails before anything renders
        set_image(cam, theme, pkg)
    takes = [s for s in edl["segments"] if s["kind"] == "source" and (only is None or s["id"] == only)]
    if not takes:
        raise SystemExit(f"no source take{' ' + only if only else ''} in edl.json")
    for seg in takes:
        matte_kind(recording_of(edl, seg))
    started = time.time()
    plan_path = os.path.join(show_dir, "Recomposite", "plan.json")
    old = json.load(open(plan_path)) if only and os.path.exists(plan_path) else {"takes": []}
    tmp = tempfile.mkdtemp(prefix="recomposite-")
    try:
        entries = [measure(edl, seg, theme, pkg, tmp) for seg in takes]
        seated = share_transforms([e for e in entries if e["found"]])
        entries = [next((x for x in seated if x["id"] == e["id"]), e) for e in entries]
        by_id = {s["id"]: s for s in takes}
        total = sum(s["out"] - s["in"] for s in takes)
        done, render_s, plan_flag, abandoned = 0, 0.0, None, None
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(workers) as pool:
            for e in entries:
                seg = by_id[e["id"]]
                ok, reason = take_ok(e["found"], SAMPLES, [], e["wanted"])
                action = on_failure(e["recorded_theme"], theme)
                e.update(status="recomposited", flag=None)
                if not ok and action == "fallback":
                    e.update(status="fallback", flag=flag(e["id"], reason, action))
                    continue
                t0 = time.time()
                areas = render_take(pool, edl, show_dir, seg, e, theme, pkg, workers, tmp)
                render_s += time.time() - t0
                done += seg["out"] - seg["in"]
                ok2, reason2 = take_ok(e["found"], SAMPLES, areas, e["wanted"])
                if not ok or not ok2:
                    e["flag"] = flag(e["id"], reason or reason2, action)
                    if action == "fallback":
                        e["status"] = "fallback"
                        os.remove(recomposite_file(show_dir, seg))
                print(f"{e['id']} [{e['cam']}, filmed on {e['recorded_theme'] or 'regular'} set]: scale {e['s']:.3f}, "
                      f"offset ({e['tx']:.0f}, {e['ty']:.0f}) -> {e['status']}{' (flagged)' if e['flag'] else ''}",
                      flush=True)
                if plan_flag is None and abandoned is None and over_budget(render_s, done, total):
                    if all(x["recorded_theme"] == theme for x in entries):
                        abandoned = (f"Re-compositing would take about {render_s / done * total / 60:.0f} min, "
                                     f"over the {BUDGET_S // 60} min budget; the show keeps its recorded picture.")
                        break
                    plan_flag = (f"Re-compositing is running slow (about {render_s / done * total / 60:.0f} min); "
                                 "it carried on because the show goes out on a different set.")
        if abandoned:
            for e in entries:
                e.update(status="fallback", flag=None)
                clip = recomposite_file(show_dir, by_id[e["id"]])
                if os.path.exists(clip):
                    os.remove(clip)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    keep_old = [t for t in old.get("takes", []) if t["id"] not in by_id]
    plan = {"theme": theme, "seconds": round(time.time() - started), "abandoned": abandoned, "flag": plan_flag,
            "takes": keep_old + [{k: e[k] for k in ("id", "recording", "cam", "recorded_theme", "s", "tx", "ty",
                                                     "status", "flag", "note", "tight")} for e in entries]}
    write_plan(show_dir, plan)
    print(f"wrote {plan_path} in {plan['seconds']} s" + (f"; ABANDONED: {abandoned}" if abandoned else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
