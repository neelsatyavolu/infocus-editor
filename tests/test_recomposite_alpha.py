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
