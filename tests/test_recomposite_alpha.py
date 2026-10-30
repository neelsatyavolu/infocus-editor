"""The anchor's per-frame alpha for re-compositing: Vision decides what is anchor, the set plate only takes pixels
away, edge pixels lose the old set's colour, and an old burned-in graphic never travels with the anchor.
Synthetic frames only."""
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import anchor_alpha  # noqa: E402

H, W = 1080, 1920


def test_anchor_inside_vision_matte_is_kept_set_is_dropped():
    rng = np.random.default_rng(1)
    plate = np.clip(120 + rng.normal(0, 3, (H, W, 3)), 0, 255).astype(np.uint8)
    frame = plate.copy()
    frame[400:800, 800:1100] = (40, 60, 200)
    vis = np.zeros((H, W), np.float32)
    vis[385:815, 785:1115] = 1                     # Vision's matte runs 15 px loose around the anchor
    a = anchor_alpha.alpha_for(frame, vis, plate, "M", chin=300)
    assert a[600, 950] > 0.95
    assert a[392, 950] < 0.05 and a[600, 792] < 0.05


def test_old_graphic_box_is_cut_from_the_alpha():
    a = np.ones((H, W), np.float32)
    out = anchor_alpha.cut_boxes(a, [(150, 840, 960, 975)])
    assert out[900, 300] == 0 and out[500, 300] == 1
    assert out[900, 300 - 160] == 0                 # padded 30 px past the box
    assert a.min() == 1                             # input untouched


def test_unmix_removes_the_old_set_from_edges():
    f, b = np.array([255.0, 0, 0]), np.array([0.0, 255, 0])
    frame = np.full((4, 4, 3), 0.5 * f + 0.5 * b, np.float32)
    behind = np.full((4, 4, 3), b, np.float32)
    out = anchor_alpha.unmix(frame, np.full((4, 4), 0.5, np.float32), behind)
    assert np.abs(out[1, 1] - f).max() <= 2


def test_steady_keeps_real_movement_and_drops_one_frame_flicker():
    base = np.zeros((4, 4), np.float32)
    flick = base.copy()
    flick[1, 1] = 1
    out = list(anchor_alpha.steady(iter([base, flick, base, base])))
    assert len(out) == 4 and out[1][1, 1] == 0


def test_green_screen_is_not_built_yet():
    assert anchor_alpha.matte_kind({"kind": "program"}) == "baked"
    with pytest.raises(NotImplementedError, match="later phase"):
        anchor_alpha.matte_kind({"kind": "green"})


def test_anchor_area_is_measured_before_old_graphics_are_cut():
    a = np.zeros((H, W), np.float32)
    a[600:1000, 200:600] = 1                        # an anchor partly under an old name bar
    cut, area = anchor_alpha.cut_and_measure(a, [(150, 840, 960, 975)])
    assert area == 400 * 400                        # the matte check sees the whole anchor
    assert cut[900, 300] == 0                       # the old plate's box is still cut out


def test_alpha_on_the_anchors_box_matches_the_full_frame():
    rng = np.random.default_rng(2)
    plate = np.clip(120 + rng.normal(0, 3, (H, W, 3)), 0, 255).astype(np.uint8)
    frame = plate.copy()
    frame[300:900, 900:1300] = (40, 60, 200)
    frame[150:300, 1000:1200] = (90, 60, 40)                # hair above the chin
    vis = np.zeros((H, W), np.float32)
    vis[140:910, 890:1310] = 1
    full = anchor_alpha.alpha_for(frame, vis, plate, "R", chin=300)
    y0, y1, x0, x1 = anchor_alpha.roi(vis)
    assert (y0, y1, x0, x1) == (92, 958, 842, 1358)                # the matte box, padded 48 px
    tex = anchor_alpha.texture(plate)[y0:y1, x0:x1]
    part = anchor_alpha.alpha_for(frame[y0:y1, x0:x1], vis[y0:y1, x0:x1], plate[y0:y1, x0:x1], "R", chin=300,
                                  origin=(y0, x0), tex=tex)
    inner = (slice(40, -40), slice(40, -40))                # away from the box's own edge
    assert np.abs(part[inner] - full[y0:y1, x0:x1][inner]).max() < 0.02


def test_roi_of_an_empty_matte_is_none():
    assert anchor_alpha.roi(np.zeros((H, W), np.float32)) is None


def test_a_box_above_and_left_of_the_crop_cuts_nothing():
    a = np.ones((100, 100), np.float32)
    assert anchor_alpha.cut_boxes(a, [(-200, -200, -40, -40)]).min() == 1


def test_without_a_plate_the_alpha_is_visions_matte():
    frame = np.full((H, W, 3), (30, 160, 60), np.uint8)    # an unknown set
    frame[400:800, 800:1100] = (40, 60, 200)
    vis = np.zeros((H, W), np.float32)
    vis[400:800, 800:1100] = 1
    a = anchor_alpha.alpha_for(frame, vis, None, "M", chin=300)
    assert a[600, 950] > 0.95 and a[600, 760] < 0.05 and a[370, 950] < 0.05   # no halo of the old set


def test_patch_takes_the_box_from_the_clean_frame_with_a_soft_edge():
    frame = np.zeros((100, 100, 3), np.float32)
    clean = np.full((100, 100, 3), 200, np.float32)
    out = anchor_alpha.patch(frame, clean, [(20, 20, 60, 60)], feather=4)
    assert out[40, 40, 0] == 200 and out[90, 90, 0] == 0          # inside from the clean frame, outside untouched
    assert 0 < out[40, 17, 0] < 200                                 # a soft edge, no seam
    assert frame.max() == 0                                         # input untouched


def _body_with_hole(inside_rgb):
    rng = np.random.default_rng(3)
    plate = np.clip(120 + rng.normal(0, 3, (H, W, 3)), 0, 255).astype(np.uint8)
    frame = plate.copy()
    frame[300:900, 700:1200] = (40, 50, 120)                 # the body
    frame[450:650, 850:1050] = inside_rgb if inside_rgb is not None else plate[450:650, 850:1050]
    vis = np.zeros((H, W), np.float32)
    vis[300:900, 700:1200] = 1
    vis[450:650, 850:1050] = 0                               # Vision missed the middle
    return frame, vis, plate


def test_a_shirt_print_vision_missed_is_filled_back_in():
    frame, vis, plate = _body_with_hole((220, 200, 160))     # a light print on the shirt
    a = anchor_alpha.alpha_for(frame, vis, plate, "M", chin=200)
    assert a[550, 950] > 0.9


def test_a_real_gap_inside_the_body_stays_open():
    frame, vis, plate = _body_with_hole(None)                # the set shows through (an arm's gap)
    a = anchor_alpha.alpha_for(frame, vis, plate, "M", chin=200)
    assert a[550, 950] < 0.1


def test_only_the_old_graphics_own_pixels_come_from_the_clean_frame():
    clean = np.full((200, 400, 3), 120, np.uint8)
    clean[60:120, 40:140] = (60, 70, 150)                    # the arm where it was before
    frame = clean.copy()
    frame[60:120, 40:140] = 120
    frame[60:120, 200:300] = (60, 70, 150)                   # the arm has moved
    frame[130:190, 20:380] = (14, 16, 14)                    # the old name bar's dark plate
    out = anchor_alpha.patch(frame, clean, [(0, 50, 400, 200)], feather=2, only_graphic=True)
    assert tuple(out[160, 200].round()) == (120, 120, 120)   # the plate is gone
    assert tuple(out[90, 250].round()) == (60, 70, 150)      # the live arm stays where it is now
    assert tuple(out[90, 90].round()) == (120, 120, 120)     # and isn't copied back where it was
