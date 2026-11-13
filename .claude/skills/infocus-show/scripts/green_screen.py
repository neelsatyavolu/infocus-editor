"""Raw green-screen camera feeds: the studio cameras recorded without the switcher's virtual set, the anchors in
front of a chroma-key green. There's no picture of the set in them at all, so they're always re-composited.

- greenness / key_mask: how key-green each pixel is (green well above red and blue)
- is_green: whether a frame (or a take's background) is a green screen
- green_camera: which camera a green frame is from, by where the people sit (two: M; one on the right: L, whose TV
  is on the left; one on the left: R)
- green_alpha / despill: the anchors' alpha by chroma key, limited to the people Vision found, and the green spill
  pulled out of their edges

No real green-screen sample existed when this was written (2026-11-13): every threshold here is a first guess from
synthetic frames and the 2026 set images, to be checked on the first real recording.
"""
import cv2
import numpy as np

GREEN_LEAD = 60                # 8-bit: how far green rises above red and blue on a well-lit key (greenness 1)
GREEN_MIN = 50                 # a key pixel's green is at least this bright (dark green-greys aren't the key)
KEY_MIN = 0.5                  # greenness from which a pixel is the key, for the share tests
GREEN_SHARE = 0.3              # a frame (or the background around the anchors) this much key is a green screen
KEY_LO, KEY_HI = 0.12, 0.7     # lead over the key's own: below LO solid anchor, above HI pure key, between soft edge
PERSON_SHARE = 0.02            # of the frame: a non-key blob at least this big is a person
TOP_MARGIN = 0.03              # of the height: a blob reaching higher than this is studio clutter, not a seated anchor
NEAR_PX = 31                   # how far past Vision's matte the key may still find hair and edges
INNER_PX = 61                  # this deep inside Vision's matte the anchor stays, even in green clothes (Vision's
                               # edge can run 15-20 px loose, so shallower would keep a halo of key)
SPILL_BAND = 10                # px either side of the matte's edge where green spill is pulled out


def lead(f):
    """Per pixel, 8-bit: how far green rises above the brighter of red and blue (0 where green is too dark to be
    the key)."""
    f = f.astype(np.int16)
    out = f[..., 1] - np.maximum(f[..., 0], f[..., 2])
    return np.where(f[..., 1] >= GREEN_MIN, np.maximum(out, 0), 0).astype(np.float32)


def greenness(f):
    """0..1 per pixel: lead() relative to a well-lit key."""
    return np.clip(lead(f) / GREEN_LEAD, 0, 1)


def key_lead(f):
    """The lead of this frame's own key (its key pixels' median): the screen's lighting varies from studio to
    studio and shot to shot, so the soft-edge ramp is measured against it."""
    ld = lead(f)
    k = ld[ld > KEY_MIN * GREEN_LEAD]
    return float(np.median(k)) if k.size else float(GREEN_LEAD)


def key_mask(f):
    return greenness(f) > KEY_MIN


def green_share(f, mask=None):
    """Share of the frame that is key, or of the pixels `mask` leaves out (the background around the anchors)."""
    k = key_mask(f)
    if mask is not None:
        k = k[~mask]
    return float(k.mean()) if k.size else 0.0


def is_green(f, mask=None):
    return green_share(f, mask) >= GREEN_SHARE


def is_green_take(frames, masks=None):
    """Whether a take's sampled frames are a green screen: the median frame's background is (so one flash or a
    frame full of anchor doesn't decide)."""
    if not frames:
        return False
    masks = masks if masks is not None else [None] * len(frames)
    return float(np.median([green_share(f, m) for f, m in zip(frames, masks)])) >= GREEN_SHARE


def people(f):
    """Centre x (0..1 of the width) of each person-sized blob that isn't key, left to right. A blob reaching the
    top of the frame is clutter beside the green screen (a light, a flat), never a seated anchor with headroom."""
    h, w = f.shape[:2]
    solid = (~key_mask(f)).astype(np.uint8)
    k = max(3, (w // 160) | 1)
    solid = cv2.morphologyEx(solid, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    n, _, stats, cents = cv2.connectedComponentsWithStats(solid, connectivity=8)
    out = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= PERSON_SHARE * h * w and stats[i, cv2.CC_STAT_TOP] > TOP_MARGIN * h:
            out.append(float(cents[i][0]) / w)
    return sorted(out)


def green_camera(f):
    """M, L or R for a green-screen frame, from its people; None when nobody is in it."""
    xs = people(f)
    if len(xs) >= 2:
        return "M"
    if len(xs) == 1:
        return "L" if xs[0] > 0.5 else "R"
    return None


def green_alpha(frame, vis, key=None):
    """Anchor alpha (0..1) on a green-screen frame. The key decides the edge (hair, fingers, soft focus); Vision's
    matte `vis` (0..1) decides who is kept (anchors, not crew or clutter beside the screen), and deep inside it the
    anchor stays even where the key would eat green clothes. `key` is the take's key_lead (else this frame's)."""
    rel = lead(frame) / max(1.0, key if key is not None else key_lead(frame))
    a_key = 1 - np.clip((rel - KEY_LO) / (KEY_HI - KEY_LO), 0, 1)
    near = cv2.GaussianBlur(cv2.dilate((vis > 0.25).astype(np.uint8), np.ones((NEAR_PX, NEAR_PX), np.uint8))
                            .astype(np.float32), (15, 15), 0)
    inner = cv2.GaussianBlur(cv2.erode((vis > 0.5).astype(np.uint8), np.ones((INNER_PX, INNER_PX), np.uint8))
                             .astype(np.float32), (9, 9), 0)
    return np.clip(np.maximum(a_key * near, inner), 0, 1).astype(np.float32)


def despill(frame, a):
    """The frame with green spill pulled out near the anchor's edge: there green is no higher than the average of
    red and blue (the brighter of them would leave a teal edge on a navy shirt). Skin and hair (green below that)
    and the anchor's middle are untouched."""
    out = frame.copy()
    edge = ((a > 0.02) & (a < 0.98)).astype(np.uint8)
    band = cv2.dilate(edge, np.ones((2 * SPILL_BAND + 1, 2 * SPILL_BAND + 1), np.uint8)) > 0
    g, rb = out[..., 1], ((out[..., 0].astype(np.uint16) + out[..., 2]) // 2).astype(out.dtype)
    out[..., 1] = np.where(band, np.minimum(g, rb), g)
    return out
