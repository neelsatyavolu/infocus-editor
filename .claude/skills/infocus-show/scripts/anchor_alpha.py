"""The anchors' alpha on one frame of a recording whose virtual set is baked in.

Vision person instances (anchor_matte) decide what is anchor, hands and fingers too, whose skin can match the set's
wood exactly. The set plate (the package PNG the switcher keyed onto, pixel for pixel) only takes pixels away: near
Vision's loose edge, a pixel within compression noise of the set is set. Raw green-screen feeds will get their own
matte later; matte_kind() is where that branch goes.
"""
import cv2
import numpy as np

from common import SCREENS

H, W = 1080, 1920
CARD_INK = (15, 17, 15)                       # announcement card background (#0F110F)
BOX_PAD = 30                                  # old plates can sit a little outside their analysis zone
ROI_PAD = 48                                  # px around Vision's matte where the anchor's edge and hair can reach


def matte_kind(recording):
    """How a recording's anchors are matted: "baked" (the set is keyed into the picture)."""
    if recording.get("kind") == "green":
        raise NotImplementedError("green-screen recordings arrive in a later phase")
    return "baked"


_TEX = {}


def texture(plate):
    """Local contrast of the set image (0-255), where compression error runs high."""
    key = id(plate)
    if key not in _TEX:
        g = cv2.cvtColor(plate, cv2.COLOR_RGB2GRAY)
        k = np.ones((5, 5), np.uint8)
        _TEX[key] = cv2.GaussianBlur((cv2.dilate(g, k) - cv2.erode(g, k)).astype(np.float32), (0, 0), 3)
    return _TEX[key]


def guided(frame, p, r=8, eps=1e-3):
    """Guided filter: snaps Vision's soft, low-res matte to the picture's own edges."""
    i = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    box = lambda x: cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1))  # noqa: E731
    mi, mp = box(i), box(p)
    a = (box(i * p) - mi * mp) / (box(i * i) - mi * mi + eps)
    return np.clip(box(a) * i + box(mp - a * mi), 0, 1)


def roi(vis, pad=ROI_PAD):
    """(y0, y1, x0, x1): the box around Vision's matte, padded, where all the per-frame work happens; None when the
    matte is empty."""
    ys = np.nonzero(vis.max(1) > 0.02)[0]
    if not len(ys):
        return None
    xs = np.nonzero(vis.max(0) > 0.02)[0]
    h, w = vis.shape
    return max(0, ys[0] - pad), min(h, ys[-1] + 1 + pad), max(0, xs[0] - pad), min(w, xs[-1] + 1 + pad)


def alpha_for(frame, vis, plate, cam, chin, origin=(0, 0), tex=None):
    """Anchor alpha (0..1). `vis` is Vision's matte (0..1), `plate` the set the frame was keyed onto, `chin` the row
    under the anchors' faces: hair above it, shoulders, arms and clothes below. The images may be a crop of the
    frame whose top-left is at `origin` (row, col), with `tex` the same crop of texture(full plate). With no plate
    (a set the package doesn't have) it is Vision's matte alone, snapped to the picture's edges."""
    if plate is None:
        return cv2.GaussianBlur(np.clip((guided(frame, vis) - 0.35) / 0.3, 0, 1), (3, 3), 0)
    d = cv2.medianBlur(np.clip(np.abs(frame.astype(np.int16) - plate.astype(np.int16)).max(2), 0, 255)
                       .astype(np.uint8), 3).astype(np.float32)
    # The recording is low-bitrate H.264, so fine set detail (the wall slats, edges) is off by more than flat areas:
    # what counts as "the same as the set" widens with the plate's local texture.
    tex = texture(plate) if tex is None else tex
    oy, ox = origin
    hh, ww = vis.shape
    person = np.clip((guided(frame, vis) - 0.35) / 0.3, 0, 1)
    differs = np.clip((d - 4 - 0.3 * tex) / 8, 0, 1)
    inner = cv2.GaussianBlur(cv2.erode((vis > 0.5).astype(np.uint8), np.ones((25, 25), np.uint8))
                             .astype(np.float32), (9, 9), 0)        # deep inside, a shirt may match the set: keep
    a = person * np.maximum(inner, differs)
    # Vision sometimes leaves a hole the body surrounds (a shirt print on some frames): there the set decides,
    # so a print that differs from the set comes back and a real gap between an arm and the body stays open.
    holes = cv2.GaussianBlur(enclosed(vis > 0.5).astype(np.float32), (5, 5), 0)
    a = np.maximum(a, holes * differs)
    up = np.clip((chin + 80 - np.arange(oy, oy + hh, dtype=np.float32)) / 80, 0, 1)[:, None]
    # Hair is see-through at its edge and can reach past Vision's: the gentle plate difference adds it back.
    near = cv2.GaussianBlur(cv2.dilate((vis > 0.25).astype(np.uint8), np.ones((13, 13), np.uint8))
                            .astype(np.float32), (15, 15), 0)
    hair = np.clip((d - 6 - 0.3 * tex) / 18, 0, 1) * near * up
    # Skin and clothes have hard edges with a thin fringe of the switcher's key spill (invisible where it was keyed,
    # plain once moved): below the chin the edge is choked 2 px.
    a = np.maximum(a * up + cv2.erode(a, np.ones((5, 5), np.uint8)) * (1 - up), hair)
    if cam in SCREENS:                                               # the TV holds a card, not the plate's blue
        x, y, w, h = SCREENS[cam]
        ty0, ty1 = max(0, y - oy), min(hh, y + h - oy)
        tx0, tx1 = max(0, x - ox), min(ww, x + w - ox)
        if ty0 < ty1 and tx0 < tx1:
            tv = (slice(ty0, ty1), slice(tx0, tx1))
            bright = np.clip((frame[tv].max(2).astype(np.float32) - 30) / 22, 0, 1)
            a[tv] = np.maximum(inner[tv], person[tv] * bright)
    return cv2.GaussianBlur(a, (3, 3), 0)


def enclosed(mask):
    """Pixels outside `mask` that it surrounds: not reachable from the image border without crossing it."""
    outside = np.pad((~mask).astype(np.uint8), 1, constant_values=1)
    reach = np.zeros((outside.shape[0] + 2, outside.shape[1] + 2), np.uint8)
    cv2.floodFill(outside, reach, (0, 0), 2)
    return outside[1:-1, 1:-1] == 1


def cut_boxes(a, boxes, pad=BOX_PAD):
    """The alpha with nothing inside each old graphic's box (x0, y0, x1, y1), padded: a plate the switcher burned
    in over the anchor stays where it was instead of moving with them (the fresh cover goes on top)."""
    out = a.copy()
    for x0, y0, x1, y1 in boxes:
        out[max(0, y0 - pad):max(0, y1 + pad), max(0, x0 - pad):max(0, x1 + pad)] = 0
    return out


def patch_mask(frame, clean, boxes, feather=8, only_graphic=False):
    """Where (0..1) `frame` should take `clean`, a frame of the same take without the old graphic: each box
    (x0, y0, x1, y1), feathered just outside so there's no seam. With only_graphic, just the pixels in the boxes that
    changed from the clean frame and look like a package graphic (dark plate, accent colour, white text), grown a
    little: an arm or hand that moved since the clean frame stays live."""
    h, w = frame.shape[:2]
    m = np.zeros((h, w), np.uint8)
    for x0, y0, x1, y1 in boxes:
        m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = 1
    if only_graphic:
        from analyze import brand_mask
        changed = np.abs(frame.astype(np.int16) - clean.astype(np.int16)).max(2) > 40
        looks = (frame.max(2) < 45) | brand_mask(frame) | (frame.min(2) > 200)
        m = m & (changed & looks).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))       # the text inside the plate
    m = cv2.dilate(m, np.ones((2 * feather + 1, 2 * feather + 1), np.uint8))
    return cv2.blur(m.astype(np.float32), (2 * feather + 1, 2 * feather + 1))


def blend(a, b, m):
    """a where m is 0, b where it is 1 (images or mattes)."""
    m = m[..., None] if a.ndim == 3 else m
    return a.astype(np.float32) * (1 - m) + b.astype(np.float32) * m


def patch(frame, clean, boxes, feather=8, only_graphic=False):
    """`frame` with the old graphic's boxes taken from `clean` (see patch_mask)."""
    return blend(frame, clean, patch_mask(frame, clean, boxes, feather, only_graphic))


def cut_and_measure(a, boxes):
    """(alpha with the old graphics cut out, anchor area in px). The area is the whole anchor's, so cutting an old
    plate out never reads as the matte jumping."""
    area = int((a > 0.5).sum())
    return (cut_boxes(a, boxes) if boxes else a), area


def unmix(frame, a, behind):
    """Foreground colour with the old set colour taken out of semi-transparent edge pixels: C = aF + (1-a)B."""
    f = frame.astype(np.float32)
    aa = a[..., None]
    fg = np.where(aa > 0.05, (f - (1 - aa) * behind.astype(np.float32)) / np.maximum(aa, 0.05), f)
    edge = (aa > 0.02) & (aa < 0.98)
    return np.where(edge, np.clip(fg, 0, 255), f)


def median3(a, b, c):
    return np.maximum(np.minimum(a, b), np.minimum(np.maximum(a, b), c))


def steady(mattes):
    """Median of each matte with its neighbours in time: Vision's edge shimmer goes, real movement stays."""
    prev = cur = None
    for nxt in mattes:
        if cur is not None:
            yield median3(prev, cur, nxt)
        prev, cur = (cur if cur is not None else nxt), nxt
    if cur is not None:
        yield median3(prev, cur, cur)
