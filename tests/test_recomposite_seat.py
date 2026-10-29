"""Seating the anchors on the virtual set: one scale and offset per take, the waist onto the table's back edge, a
tight camera's bottom cut kept behind the table's front panel, and takes from one camera sharing a transform.
Synthetic silhouettes only."""
import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import seat  # noqa: E402
import virtual_set  # noqa: E402

H, W = 1080, 1920
NO_TOUCH = {"left": False, "right": False, "top": False, "bottom": False}


def torso(top, bottom, left, right):
    a = np.zeros((H, W), np.float32)
    a[top:bottom, left:right] = 1
    return a


def test_waist_lands_on_the_table_back_edge():
    geom = virtual_set.geometry("R", "")
    a = torso(400, 1000, 1300, 1700)
    s, tx, ty, _ = seat.fit(geom, [[[1450, 330, 100, 130]]], NO_TOUCH, [a])
    (cx, waist), = seat.waists([a], [1500])
    placed = s * waist + ty
    assert abs(placed - (seat.edge_y(geom["desk_back"], s * cx + tx) + seat.WAIST_TUCK)) <= 6


def test_tight_two_shot_never_shows_its_bottom_cut():
    geom = virtual_set.geometry("M", "")
    a = np.maximum(torso(500, H, 400, 760), torso(500, H, 1180, 1540))
    boxes = [[[530, 420, 110, 140], [1310, 420, 110, 140]]]
    s, tx, ty, _ = seat.fit(geom, boxes, {**NO_TOUCH, "bottom": True}, [a])
    cols = np.nonzero(a[H - 3:].any(0))[0].astype(np.float32)
    lip = seat.edge_y(geom["desk_lip"], s * cols + tx)
    assert (s * (H - 1) + ty >= lip + seat.LIP_PANEL - 0.5).all()


def test_well_seated_shot_is_left_alone():
    geom = virtual_set.geometry("R", "")
    seat_x, seat_h = geom["seats"]["x"][0], geom["seats"]["h"]
    waist = round(float(seat.edge_y(geom["desk_back"], seat_x)) + seat.WAIST_TUCK)
    a = torso(300, waist + 1, seat_x - 190, seat_x + 190)    # bottom row = the back edge under this anchor
    box = [[seat_x - 50, 260, 100, seat_h]]
    s, tx, ty, note = seat.fit(geom, [box], NO_TOUCH, [a])
    assert (s, tx, ty) == (1.0, 0.0, 0.0), note


def test_no_faces_leaves_the_shot_alone():
    s, tx, ty, note = seat.fit(virtual_set.geometry("M", ""), [[]], NO_TOUCH, [torso(400, 1000, 400, 800)])
    assert (s, tx, ty) == (1.0, 0.0, 0.0) and "no faces" in note


def test_edges_touched_reports_frame_edge_cuts():
    touch = seat.edges_touched([torso(400, H, 0, 500)])
    assert touch == {"left": True, "right": False, "top": False, "bottom": True}


def test_close_transforms_share_their_median():
    takes = [{"id": f"t{i}", "recording": "r1", "cam": "R", "s": s, "tx": tx, "ty": 20.0}
             for i, (s, tx) in enumerate([(1.00, 10.0), (1.01, 14.0), (0.99, 12.0)])]
    out = seat.share_transforms(takes)
    assert [(t["s"], t["tx"], t["ty"]) for t in out] == [(1.00, 12.0, 20.0)] * 3
    assert takes[0]["tx"] == 10.0                       # inputs untouched


def test_an_outlier_take_keeps_its_own_transform():
    takes = [{"id": f"t{i}", "recording": "r1", "cam": "R", "s": 1.0, "tx": tx, "ty": 0.0}
             for i, tx in enumerate([10.0, 14.0, 12.0, 60.0])]
    out = seat.share_transforms(takes)
    assert out[3]["tx"] == 60.0
    assert {t["tx"] for t in out[:3]} == {12.0}      # the median of the close takes


def test_other_cameras_and_recordings_are_grouped_apart():
    takes = [{"id": "a", "recording": "r1", "cam": "L", "s": 1.0, "tx": 0.0, "ty": 0.0},
             {"id": "b", "recording": "r1", "cam": "R", "s": 1.0, "tx": 6.0, "ty": 0.0},
             {"id": "c", "recording": "r2", "cam": "L", "s": 1.0, "tx": 4.0, "ty": 0.0}]
    assert [t["tx"] for t in seat.share_transforms(takes)] == [0.0, 6.0, 4.0]
