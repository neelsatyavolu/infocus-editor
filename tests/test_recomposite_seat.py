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


def seated(top, bottom, left, right):
    """A body down to `bottom` with forearms and hands below it, narrower: how a real anchor tapers (not a crop)."""
    a = torso(top, bottom, left, right)
    m = (left + right) // 2
    a[bottom:bottom + 60, m - (right - left) // 5:m + (right - left) // 5] = 1
    return a


def test_waist_lands_on_the_table_back_edge():
    geom = virtual_set.geometry("R", "")
    a = seated(400, 1000, 1300, 1700)
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
    a = seated(300, waist + 1, seat_x - 190, seat_x + 190)   # waist row = the back edge under this anchor
    box = [[seat_x - 50, 260, 100, seat_h]]
    s, tx, ty, note = seat.fit(geom, [box], NO_TOUCH, [a])
    (t,) = seat.settle([{"id": "a", "recording": "r1", "cam": "R", "s": s, "tx": tx, "ty": ty}])
    assert (t["s"], t["tx"], t["ty"]) == (1.0, 0.0, 0.0), note


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


def test_takes_either_side_of_the_snap_share_one_size():
    takes = [{"id": "a", "recording": "r1", "cam": "R", "s": 0.905, "tx": 10.0, "ty": 0.0},
             {"id": "b", "recording": "r1", "cam": "R", "s": 0.895, "tx": 12.0, "ty": 0.0}]
    out = seat.settle(seat.share_transforms(takes))
    assert out[0]["s"] == out[1]["s"]


def test_spread_out_takes_still_share_their_median():
    takes = [{"id": f"t{i}", "recording": "r1", "cam": "M", "s": 1.0, "tx": tx, "ty": 0.0}
             for i, tx in enumerate([0.0, 10.0, 20.0])]
    assert {t["tx"] for t in seat.share_transforms(takes)} == {10.0}


def test_settle_leaves_a_nearly_seated_camera_alone():
    out = seat.settle([{"id": "a", "recording": "r1", "cam": "R", "s": 0.97, "tx": 12.0, "ty": -3.0}])
    assert (out[0]["s"], out[0]["tx"], out[0]["ty"]) == (1.0, 0.0, 0.0)


def test_the_waist_is_where_the_body_ends_not_where_the_hands_do():
    a = torso(400, 900, 1300, 1700)                  # the body, down to where the real desk hides it
    a[900:1000, 1420:1580] = 1                       # forearms and clasped hands on the desk, in front
    (cx, waist), = seat.waists([a], [1500])
    assert abs(waist - 899) <= 2


def test_the_waist_is_the_switchers_crop_line():
    a = torso(400, 840, 1300, 1700)                  # the switcher cropped the key straight across at 840
    a[840:850, 1480:1520] = 1                        # a few stray pixels below it
    (cx, waist), = seat.waists([a], [1500])
    assert abs(waist - 839) <= 2


def test_arms_spread_wider_than_the_body_end_at_their_crop():
    a = torso(400, 990, 1400, 1600)                  # a narrow body
    a[850:990, 1250:1750] = 1                        # arms spread wide on the desk, cropped at 990
    (cx, waist), = seat.waists([a], [1500])
    assert abs(waist - 989) <= 2


def test_elbows_land_on_the_table_back_edge():
    geom = virtual_set.geometry("M", "")
    a = np.maximum(seated(400, 990, 400, 760), seated(400, 990, 1180, 1540))
    boxes = [[[530, 300, 110, 215], [1310, 300, 110, 215]]]
    elbows = [(580.0, 860.0), (1360.0, 860.0)]
    s, tx, ty, _ = seat.fit(geom, boxes, NO_TOUCH, [a], rest=elbows)
    for cx, y in elbows:
        assert abs(s * y + ty - (seat.edge_y(geom["desk_back"], s * cx + tx) + seat.ELBOW_DROP)) <= 6


def test_a_switcher_crop_never_lands_below_the_back_edge():
    geom = virtual_set.geometry("M", "")
    a = np.maximum(torso(400, 840, 400, 760), torso(400, 840, 1180, 1540))   # cropped straight across at 840
    boxes = [[[530, 300, 110, 215], [1310, 300, 110, 215]]]
    s, tx, ty, _ = seat.fit(geom, boxes, NO_TOUCH, [a], rest=[(580.0, 820.0), (1360.0, 820.0)])
    for x in (400, 760, 1180, 1540):
        assert s * 839 + ty <= seat.edge_y(geom["desk_back"], s * x + tx) + seat.WAIST_TUCK + 1


def test_a_crop_is_found_under_a_few_stray_pixels():
    a = torso(400, 846, 400, 1500) > 0                 # cropped straight across
    a[846:900, 700:760] = True                          # a scrap of an old name bar under it
    assert abs(seat.crop_row(a) - 845) <= 2


def test_forearms_tapering_below_the_body_are_not_a_crop():
    a = seated(400, 940, 400, 1500) > 0
    assert seat.crop_row(a) is None


def test_hands_stay_on_the_table_top_in_front_of_the_panel():
    geom = virtual_set.geometry("L", "")
    a = seated(400, 960, 1300, 1700)
    rest = [(1500.0, 850.0, 1010.0)]                    # leaning in: forearms run toward the camera
    s, tx, ty, _ = seat.fit(geom, [[[1450, 280, 100, 250]]], NO_TOUCH, [a], rest=rest)
    assert s * 1010 + ty <= seat.edge_y(geom["desk_lip"], s * 1500 + tx) - seat.HAND_MARGIN + 1


def test_a_shot_already_cut_at_the_bottom_is_never_enlarged():
    geom = virtual_set.geometry("R", "")
    a = torso(500, H, 300, 700)
    s, _, _, _ = seat.fit(geom, [[[450, 380, 80, 200]]], {**NO_TOUCH, "bottom": True}, [a])
    assert s <= 1.0
