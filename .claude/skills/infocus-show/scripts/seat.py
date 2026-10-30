"""Seating the anchors on the virtual set: one scale + offset per take (the cameras are locked off).

Across, the anchors' faces go onto the set's seats. Up and down, each anchor's waist (read from the elbows, since
the real desk hides it) goes onto the back edge of the table, with the arms and hands resting on the table top in
front. A tight camera is shrunk to keep headroom, and an edge where the camera cut an anchor off is never pulled into
view. Takes from the same camera of one recording that measured about the same share one transform, so the anchors
don't creep between cuts.
"""
import cv2
import numpy as np

H, W = 1080, 1920
HEADROOM = 60                                 # px above the highest head once seated; shrink if a tight camera needs it
WAIST_TUCK = 4                                # px of the waist set just behind the back edge
FRAME_MARGIN = 12                             # px an anchor who was fully in frame keeps from its edges
TV_CLEAR = 20                                 # px a side-set anchor's head and shoulders keep from the TV screen
HAND_MARGIN = 30                              # px the wrists stay above the table's front lip, hands on the table top
LIP_PANEL = 5                                 # px from the lip's highlight down to the front panel
SETTLED = {"s": 0.10, "x": 30, "y": 4}       # a camera this close to seated is left alone (no needless resampling)
SCALE_RANGE = (0.8, 1.25)
SHARE_PX, SHARE_SCALE = 24, 0.04              # takes this close to their camera's median share it


def edge_y(curve, x):
    pts = np.array(curve, np.float32)
    return np.interp(x, pts[:, 0], pts[:, 1])


def waists(alphas, face_x):
    """Each anchor's waist: where their body ends, behind the desk, and only the forearms and hands carry on in
    front (or where the switcher cropped the key). Read from the anchor's own columns (those nearest their face):
    below the shoulders, the first row under the widest one where the silhouette narrows to under 80% of it.
    Returns [(centre x, waist row, leftmost and rightmost column of the body at that row)] per anchor."""
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
        own = sil[:, run]
        width = own.sum(1)
        rows = np.nonzero(width)[0]
        top, bottom = int(rows[0]), int(rows[-1])
        start = top + int(0.4 * (bottom - top))                      # below the head and shoulders
        widest = start + int(np.argmax(width[start:bottom + 1]))
        narrow = np.nonzero(width[widest:bottom + 1] < 0.8 * width[widest])[0]
        waist = widest + int(narrow[0]) - 1 if len(narrow) else bottom
        at = run[own[waist]] if own[waist].any() else run
        out.append((float(run.mean()), float(waist), float(at.min()), float(at.max())))
    return out


def edges_touched(mattes):
    """Which frame edges the anchors' silhouette runs off (where the camera cut them). Scraps much smaller than the
    anchors (a neighbour's fingertip at the edge) don't count."""
    sil = big_parts(np.max(np.stack(mattes), 0) > 0.5)
    run = 40                                                         # px of silhouette along an edge that count
    return {"left": int(sil[:, :3].any(1).sum()) > run, "right": int(sil[:, -3:].any(1).sum()) > run,
            "top": int(sil[:3].any(0).sum()) > run, "bottom": int(sil[-3:].any(0).sum()) > run}


def big_parts(sil):
    """The silhouette's big parts: the anchors, not a scrap of a neighbour's hand or a stray matte speck."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(sil.astype(np.uint8), connectivity=8)
    if n <= 2:
        return sil
    areas = stats[1:, cv2.CC_STAT_AREA]
    return np.isin(labels, [i + 1 for i, a in enumerate(areas) if a >= 0.1 * areas.max()])


def body_extent(sil):
    """(left, right) columns of the anchors themselves (big_parts)."""
    xs = np.nonzero(big_parts(sil).any(0))[0]
    return float(xs.min()), float(xs.max())


def desk_line(sil, cols, tol=5.0):
    """Where the real desk hides the anchor, as (columns, row on that line per column): the straight line (it can
    slant in a side camera) that most of the anchor's column bottoms sit on. Forearms on the desk end below it and a
    raised elbow or hair above it; neither counts. Empty arrays when no line is clear."""
    cols = np.asarray([c for c in cols if sil[:, c].any()])
    if len(cols) < 30:
        return np.array([]), np.array([])
    bottoms = np.array([np.nonzero(sil[:, c])[0].max() for c in cols], np.float32)
    rng = np.random.default_rng(0)
    best = None
    for _ in range(300):
        i, j = rng.choice(len(cols), 2, replace=False)
        if abs(int(cols[i]) - int(cols[j])) < 40:
            continue
        k = (bottoms[j] - bottoms[i]) / (cols[j] - cols[i])
        inl = np.abs(bottoms - (bottoms[i] + k * (cols - cols[i]))) <= tol
        if best is None or inl.sum() > best.sum():
            best = inl
    if best is None or best.sum() < 30:
        return np.array([]), np.array([])
    k, b = np.polyfit(cols[best], bottoms[best], 1)
    inl = np.abs(bottoms - (k * cols + b)) <= tol
    return cols[inl], (k * cols[inl] + b).astype(np.float32)


def crop_row(sil):
    """The row the switcher's key was cropped straight across at (the body ends in a wide, flat edge above the frame
    bottom), or None when the silhouette tapers off (forearms and hands) or runs off the frame."""
    widths = sil.sum(1)
    if not widths.max():
        return None
    wide = np.nonzero(widths >= 0.5 * widths.max())[0]
    last = int(wide[-1])
    if last >= H - 3:
        return None
    return last if widths[min(H - 1, last + 6)] < 0.25 * widths.max() else None


def lowest_edge(curve, x0, x1):
    """The lowest point (largest row) of a table edge between columns x0 and x1."""
    return float(np.max(edge_y(curve, np.linspace(x0, x1, 16))))


def fit(geom, boxes, touch, alphas, rest=None):
    """(scale, tx, ty, note) for one take. `geom` is virtual_set.geometry(cam, theme); `boxes` the face boxes
    [x, y, w, h] found on each sampled frame; `touch` from edges_touched; `alphas` the sampled anchor alphas;
    `rest` each anchor's [(x, elbow row, wrist row or None, (left, right) shoulders or None)] from body pose.
    Where each anchor's body ends (the waist, or the switcher's crop) goes on the table's back edge, under their
    body, with the forearms and hands in front on the table top; never so low that the hands go behind the table's
    front panel. An anchor who was fully in frame stays in it. A shot the camera already cut off at the bottom is
    never enlarged."""
    seat = geom["seats"]
    n = len(seat["x"])
    per = [sorted(b, key=lambda r: -r[2])[:n] for b in boxes if len(b) >= n]
    per = [sorted(b, key=lambda r: r[0]) for b in per]
    if not per:
        return 1.0, 0.0, 0.0, "no faces found; left as is"
    arr = np.array(per, np.float32)                                  # samples x anchors x (x, y, w, h)
    cx = np.median(arr[:, :, 0] + arr[:, :, 2] / 2, 0)
    ws = waists(alphas, cx)
    wrists = [(r[0], r[2]) for r in rest or [] if len(r) > 2 and r[2] is not None]
    if not ws:
        return 1.0, 0.0, 0.0, "no anchor found; left as is"
    fh = float(np.median(arr[:, :, 3]))
    wanted = seat["h"] / fh
    s = float(np.clip(wanted, *SCALE_RANGE))
    if touch["bottom"]:
        s = min(s, 1.0)
    sil = np.max(np.stack(alphas), 0) > 0.5
    top = float(np.percentile(np.nonzero(sil.any(1))[0], 1))
    bottom_cols = np.nonzero(sil[H - 3:].any(0))[0].astype(np.float32)
    crop = crop_row(sil)
    crop_cols = np.nonzero(sil[crop])[0].astype(np.float32) if crop is not None else None
    left, right = body_extent(sil)
    # Where the real desk hid each anchor must reach the virtual table everywhere along it: the line can slant (a
    # side camera, an anchor leaning in) and the table's edge curves, and any part above it would float.
    every = np.nonzero(sil.sum(0) > 0)[0]
    owner = np.argmin(np.abs(every[:, None] - np.asarray(cx)[None, :]), 1)
    lines = [desk_line(sil, every[owner == i]) for i in range(len(cx))]
    body_cols = np.concatenate([c for c, _ in lines]).astype(np.float32) if lines else np.array([])
    bottoms = np.concatenate([b for _, b in lines]) if lines else np.array([])
    screen = geom.get("screen")
    beside = None                                                    # the anchor's edge next to the TV, at its height
    if screen:
        sx, sy, sw, sh = screen
        tv_rows = np.nonzero(sil[sy:sy + sh].any(1))[0]
        if len(tv_rows):
            cols = np.nonzero(sil[sy:sy + sh].any(0))[0]
            beside = float(cols.min()) if sx + sw / 2 < W / 2 else float(cols.max())
    back, lip = geom["desk_back"], geom["desk_lip"]

    def place(s):
        tx = float(np.mean(seat["x"]) - s * np.mean(cx))
        # The camera cut the anchor off at a side of the frame: that side must stay outside it, or the cut shows.
        if touch["left"]:
            tx = min(tx, 0.0)
        if touch["right"]:
            tx = max(tx, (W - 1) * (1 - s))
        if beside is not None:                                       # off the TV as far as the frame allows
            sx, sy, sw, sh = screen
            if sx + sw / 2 < W / 2:
                tx = max(tx, sx + sw + TV_CLEAR - s * beside)
            else:
                tx = min(tx, sx - TV_CLEAR - s * beside)
        if not touch["right"]:                                       # fully in frame: stay in it
            tx = min(tx, W - 1 - FRAME_MARGIN - s * right)
        if not touch["left"]:
            tx = max(tx, FRAME_MARGIN - s * left)
        # The body's bottom goes on the table's back edge, at its lowest point under the body: the side sets' edge
        # slopes, and anywhere it sits lower than the body a gap of the set would show under the shirt.
        ty = float(np.mean([lowest_edge(back, s * a + tx, s * b + tx) + WAIST_TUCK - s * w for _, w, a, b in ws]))
        for x, w in wrists:                                          # the hands stay on the table top
            ty = min(ty, float(edge_y(lip, s * x + tx)) - HAND_MARGIN - s * w)
        if len(body_cols):                                           # no part of the body floats above the table
            ty = max(ty, float(np.max(edge_y(back, s * body_cols + tx) - s * bottoms)) + WAIST_TUCK)
        if crop_cols is not None:                                    # a switcher crop never floats above the table
            ty = max(ty, float(np.max(edge_y(back, s * crop_cols + tx))) + WAIST_TUCK - s * crop)
        if len(bottom_cols):                                         # a cut at the frame bottom stays behind the panel
            ty = max(ty, float(np.max(edge_y(lip, s * bottom_cols + tx))) + LIP_PANEL - s * (H - 1))
        return tx, ty

    tx, ty = place(s)
    if s * top + ty < HEADROOM:                                      # a tight camera: seated, the heads hit the top
        w = np.mean([r[1] for r in ws])
        s = float(np.clip((np.mean([edge_y(back, r[0]) for r in ws]) + WAIST_TUCK - HEADROOM) / (w - top),
                          SCALE_RANGE[0], s))
        tx, ty = place(s)
    note = (f"faces h={fh:.0f} at x={[round(float(v)) for v in cx]}; waists at y={[round(r[1]) for r in ws]}; "
            f"crop {crop}; "
            f"wanted scale {wanted:.3f}")
    return s, tx, ty, note + f"; touches {[k for k, v in touch.items() if v]}"


def settle(takes):
    """Takes whose (shared) transform is within SETTLED of none at all keep the recording's own framing. Run it
    after share_transforms, so takes from one camera settle together. Returns new dicts."""
    out = []
    for t in takes:
        near = abs(t["s"] - 1) < SETTLED["s"] and abs(t["tx"]) < SETTLED["x"] and abs(t["ty"]) < SETTLED["y"]
        out.append({**t, "s": 1.0, "tx": 0.0, "ty": 0.0} if near else dict(t))
    return out


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
