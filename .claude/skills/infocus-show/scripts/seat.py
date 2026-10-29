"""Seating the anchors on the virtual set: one scale + offset per take (the cameras are locked off).

Across, the anchors' faces go onto the set's seats. Up and down, each anchor's waist (read from the elbows, since
the real desk hides it) goes onto the back edge of the table, with the arms and hands resting on the table top in
front. A tight camera is shrunk to keep headroom, and an edge where the camera cut an anchor off is never pulled into
view. Takes from the same camera of one recording that measured about the same share one transform, so the anchors
don't creep between cuts.
"""
import numpy as np

H, W = 1080, 1920
HEADROOM = 60                                 # px above the highest head once seated; shrink if a tight camera needs it
WAIST_TUCK = 4                                # px of the waist set just behind the back edge
LIP_PANEL = 5                                 # px from the lip's highlight down to the front panel
TOL = {"s": 0.10, "x": 30, "y": 25}          # inside these the take is left alone (no needless resampling)
SCALE_RANGE = (0.8, 1.25)
SHARE_PX, SHARE_SCALE = 8, 0.02               # takes this close to their camera's median share it


def edge_y(curve, x):
    pts = np.array(curve, np.float32)
    return np.interp(x, pts[:, 0], pts[:, 1])


def waists(alphas, face_x):
    """Each anchor's waist: where the real desk hid their body. The desk hides the waist itself, so it is read
    from the elbows, which rest on the desk beside the body while the hands reach forward: the silhouette's bottom
    10-25% in from each side of the anchor (the outermost columns are the sleeve's rounded edge). Columns belong to
    the nearest face. Returns [(centre x, waist row)] per anchor."""
    sil = np.max(np.stack(alphas), 0) > 0.5
    cols = np.nonzero(sil.sum(0) > 150)[0]
    if not len(cols):
        return []
    owner = np.argmin(np.abs(cols[:, None] - np.asarray(face_x)[None, :]), 1)
    out = []
    for i in range(len(face_x)):
        run = cols[owner == i]
        if len(run) < 100:
            continue
        lo, hi = run.min(), run.max()
        span = hi - lo
        band = run[((run >= lo + 0.10 * span) & (run <= lo + 0.25 * span)) |
                   ((run >= hi - 0.25 * span) & (run <= hi - 0.10 * span))]
        bottom = [np.nonzero(sil[:, c])[0].max() for c in band]
        out.append((float(run.mean()), float(np.median(bottom))))
    return out


def edges_touched(mattes):
    """Which frame edges the anchors' silhouette runs off (where the camera cut them)."""
    sil = np.max(np.stack(mattes), 0) > 0.5
    run = 40                                                         # px of silhouette along an edge that count
    return {"left": int(sil[:, :3].any(1).sum()) > run, "right": int(sil[:, -3:].any(1).sum()) > run,
            "top": int(sil[:3].any(0).sum()) > run, "bottom": int(sil[-3:].any(0).sum()) > run}


def fit(geom, boxes, touch, alphas):
    """(scale, tx, ty, note) for one take. `geom` is virtual_set.geometry(cam, theme); `boxes` the face boxes
    [x, y, w, h] found on each sampled frame; `touch` from edges_touched; `alphas` the sampled anchor alphas."""
    seat = geom["seats"]
    n = len(seat["x"])
    per = [sorted(b, key=lambda r: -r[2])[:n] for b in boxes if len(b) >= n]
    per = [sorted(b, key=lambda r: r[0]) for b in per]
    if not per:
        return 1.0, 0.0, 0.0, "no faces found; left as is"
    arr = np.array(per, np.float32)                                  # samples x anchors x (x, y, w, h)
    cx = np.median(arr[:, :, 0] + arr[:, :, 2] / 2, 0)
    ws = waists(alphas, cx)
    if not ws:
        return 1.0, 0.0, 0.0, "no anchor found; left as is"
    fh = float(np.median(arr[:, :, 3]))
    wanted = seat["h"] / fh
    s = 1.0 if abs(wanted - 1) < TOL["s"] else wanted
    s = float(np.clip(s, *SCALE_RANGE))
    sil = np.max(np.stack(alphas), 0) > 0.5
    top = float(np.percentile(np.nonzero(sil.any(1))[0], 1))
    bottom_cols = np.nonzero(sil[H - 3:].any(0))[0].astype(np.float32)
    back, lip = geom["desk_back"], geom["desk_lip"]

    def place(s):
        tx = float(np.mean(seat["x"]) - s * np.mean(cx))
        # The camera cut the anchor off at a side of the frame: that side must stay outside it, or the cut shows.
        if touch["left"]:
            tx = min(tx, 0.0)
        if touch["right"]:
            tx = max(tx, (W - 1) * (1 - s))
        ty = float(np.mean([edge_y(back, s * x + tx) + WAIST_TUCK - s * w for x, w in ws]))
        if len(bottom_cols):                                         # a cut at the frame bottom stays behind the panel
            ty = max(ty, float(np.max(edge_y(lip, s * bottom_cols + tx))) + LIP_PANEL - s * (H - 1))
        return tx, ty

    tx, ty = place(s)
    if s * top + ty < HEADROOM:                                      # a tight camera: seated, the heads hit the top
        w = np.mean([w for _, w in ws])
        s = float(np.clip((np.mean([edge_y(back, x) for x, _ in ws]) + WAIST_TUCK - HEADROOM) / (w - top),
                          SCALE_RANGE[0], s))
        tx, ty = place(s)
    note = (f"faces h={fh:.0f} at x={[round(float(v)) for v in cx]}; waists at y={[round(w) for _, w in ws]}; "
            f"wanted scale {wanted:.3f}")
    if s == 1.0 and abs(tx) < TOL["x"] and abs(ty) < 4:
        return 1.0, 0.0, 0.0, note + "; already seated, left as is"
    return s, tx, ty, note + f"; touches {[k for k, v in touch.items() if v]}"


def share_transforms(takes):
    """Takes ({recording, cam, s, tx, ty, ...}) from the same recording and camera whose transforms all sit within
    SHARE_PX / SHARE_SCALE of their group's median use that median; the rest keep their own. Returns new dicts."""
    out = [dict(t) for t in takes]
    groups = {}
    for i, t in enumerate(out):
        groups.setdefault((t["recording"], t["cam"]), []).append(i)
    for members in groups.values():
        med = {k: float(np.median([out[i][k] for i in members])) for k in ("s", "tx", "ty")}
        close = [i for i in members if abs(out[i]["s"] - med["s"]) <= SHARE_SCALE
                 and abs(out[i]["tx"] - med["tx"]) <= SHARE_PX and abs(out[i]["ty"] - med["ty"]) <= SHARE_PX]
        if len(close) < 2:
            continue
        shared = {k: float(np.median([out[i][k] for i in close])) for k in ("s", "tx", "ty")}
        for i in close:
            out[i].update(shared)
    return out
