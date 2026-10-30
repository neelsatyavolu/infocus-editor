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
    (cx, waist, _, _), = seat.waists([a], [1500])
    placed = s * waist + ty
    assert abs(placed - (seat.lowest_edge(geom["desk_back"], s * 1300 + tx, s * 1699 + tx) + seat.WAIST_TUCK)) <= 6


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
    waist = round(seat.lowest_edge(geom["desk_back"], seat_x - 190, seat_x + 189) + seat.WAIST_TUCK)
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
    (cx, waist, _, _), = seat.waists([a], [1500])
    assert abs(waist - 899) <= 2


def test_the_waist_is_the_switchers_crop_line():
    a = torso(400, 840, 1300, 1700)                  # the switcher cropped the key straight across at 840
    a[840:850, 1480:1520] = 1                        # a few stray pixels below it
    (cx, waist, _, _), = seat.waists([a], [1500])
    assert abs(waist - 839) <= 2


def test_arms_spread_wider_than_the_body_end_at_their_crop():
    a = torso(400, 990, 1400, 1600)                  # a narrow body
    a[850:990, 1250:1750] = 1                        # arms spread wide on the desk, cropped at 990
    (cx, waist, _, _), = seat.waists([a], [1500])
    assert abs(waist - 989) <= 2


def test_the_body_bottom_lands_on_the_back_edge_even_with_a_pose():
    """The reference shows: where the torso ends (the forearms carry on in front) sits on the table's back edge."""
    geom = virtual_set.geometry("M", "")
    a = np.maximum(seated(400, 930, 400, 760), seated(400, 930, 1180, 1540))
    boxes = [[[530, 300, 110, 215], [1310, 300, 110, 215]]]
    rest = [(580.0, 860.0, 980.0, (430.0, 730.0)), (1360.0, 860.0, 980.0, (1210.0, 1510.0))]
    s, tx, ty, _ = seat.fit(geom, boxes, NO_TOUCH, [a], rest=rest)
    for lo, hi in ((400, 759), (1180, 1539)):
        low = seat.lowest_edge(geom["desk_back"], s * lo + tx, s * hi + tx)
        assert abs(s * 929 + ty - (low + seat.WAIST_TUCK)) <= 8


def test_a_switcher_crop_sits_on_the_back_edge():
    geom = virtual_set.geometry("M", "")
    a = np.maximum(torso(400, 840, 400, 760), torso(400, 840, 1180, 1540))   # cropped straight across at 840
    boxes = [[[530, 300, 110, 215], [1310, 300, 110, 215]]]
    s, tx, ty, _ = seat.fit(geom, boxes, NO_TOUCH, [a], rest=[(580.0, 820.0, None, None), (1360.0, 820.0, None, None)])
    for x in (580, 1360):                                # under each anchor's body
        assert abs(s * 839 + ty - (seat.edge_y(geom["desk_back"], s * x + tx) + seat.WAIST_TUCK)) <= 10


def test_a_crop_is_found_under_a_few_stray_pixels():
    a = torso(400, 846, 400, 1500) > 0                 # cropped straight across
    a[846:900, 700:760] = True                          # a scrap of an old name bar under it
    assert abs(seat.crop_row(a) - 845) <= 2


def test_forearms_tapering_below_the_body_are_not_a_crop():
    a = seated(400, 940, 400, 1500) > 0
    assert seat.crop_row(a) is None


def test_hands_stay_on_the_table_top_in_front_of_the_panel():
    geom = virtual_set.geometry("L", "")
    a = seated(400, 960, 1400, 1600)
    rest = [(1500.0, 850.0, 980.0, None)]               # hands just in front of the body, on the desk
    s, tx, ty, _ = seat.fit(geom, [[[1450, 280, 100, 250]]], NO_TOUCH, [a], rest=rest)
    assert s * 980 + ty <= seat.edge_y(geom["desk_lip"], s * 1500 + tx) - seat.HAND_MARGIN + 1


def test_a_shot_already_cut_at_the_bottom_is_never_enlarged():
    geom = virtual_set.geometry("R", "")
    a = torso(500, H, 300, 700)
    s, _, _, _ = seat.fit(geom, [[[450, 380, 80, 200]]], {**NO_TOUCH, "bottom": True}, [a])
    assert s <= 1.0


def test_an_anchor_in_frame_is_never_pushed_out_of_it():
    geom = virtual_set.geometry("L", "")
    a = seated(350, 980, 1150, 1880)                   # a wide anchor, fully in frame, face left of the seat
    s, tx, ty, _ = seat.fit(geom, [[[1250, 250, 200, 252]]], NO_TOUCH, [a])
    assert s * 1879 + tx <= W - seat.FRAME_MARGIN + 1


def test_a_crop_sits_on_the_table_where_it_is_lowest():
    geom = virtual_set.geometry("L", "")               # its back edge drops from ~930 on the left to 1000 on the right
    a = torso(400, 1040, 1500, 1800)                   # the body, cropped straight across at 1040
    s, tx, ty, _ = seat.fit(geom, [[[1600, 280, 100, 252]]], NO_TOUCH, [a])
    low = seat.lowest_edge(geom["desk_back"], s * 1500 + tx, s * 1799 + tx)
    assert abs(s * 1039 + ty - (low + seat.WAIST_TUCK)) <= 8


def test_a_side_anchor_moves_off_the_tv_while_staying_in_frame():
    geom = virtual_set.geometry("L", "")                # its TV is on the left, up to x = 55 + 1124
    a = seated(300, 980, 1150, 1650)                   # hair and shoulders over the TV's right edge
    s, tx, ty, _ = seat.fit(geom, [[[1300, 250, 200, 252]]], NO_TOUCH, [a])
    x, y, w, h = geom["screen"]
    assert s * 1150 + tx >= x + w + seat.TV_CLEAR - 1
    assert s * 1649 + tx <= W - seat.FRAME_MARGIN + 1


def test_frame_wins_over_clearing_the_tv():
    geom = virtual_set.geometry("L", "")
    a = seated(300, 980, 1000, 1900)                   # too wide to clear the TV and stay in frame
    s, tx, ty, _ = seat.fit(geom, [[[1300, 250, 200, 252]]], NO_TOUCH, [a])
    assert s * 1899 + tx <= W - seat.FRAME_MARGIN + 1


def test_a_stray_scrap_at_the_frame_edge_doesnt_hold_the_anchor_back():
    geom = virtual_set.geometry("L", "")
    a = seated(300, 980, 1100, 1650)
    a[900:980, 1880:1920] = 1                          # a neighbour's fingertips at the frame edge
    s, tx, ty, _ = seat.fit(geom, [[[1250, 250, 200, 252]]], NO_TOUCH, [a])
    x, y, w, h = geom["screen"]
    assert s * 1100 + tx >= x + w + seat.TV_CLEAR - 1   # free to clear the TV


def test_a_shot_cut_at_the_bottom_is_never_enlarged_even_with_a_pose():
    geom = virtual_set.geometry("R", "")
    a = torso(500, H, 300, 700)
    rest = [(500.0, 900.0, 1010.0, (350.0, 650.0))]
    s, _, _, _ = seat.fit(geom, [[[450, 380, 80, 200]]], {**NO_TOUCH, "bottom": True}, [a], rest=rest)
    assert s <= 1.0


def test_the_body_bottom_never_floats_above_a_curving_table():
    """The side sets' back edge slopes; under the whole width of the body's bottom it must be at or below the
    edge, or a gap of the set shows under the shirt."""
    for cam, body in (("L", (1350, 1880)), ("R", (40, 640))):
        geom = virtual_set.geometry(cam, "")
        a = torso(400, 1000, *body)                    # cropped straight across at 1000
        cx = sum(body) / 2
        s, tx, ty, _ = seat.fit(geom, [[[cx - 60, 250, 120, 252]]], NO_TOUCH, [a])
        for x in range(body[0], body[1], 20):
            assert s * 999 + ty >= seat.edge_y(geom["desk_back"], s * x + tx) - 1, (cam, x)


def test_a_slanting_hem_never_floats_above_the_table():
    """Leaning in, the hem at the back of the body sits higher than at the front: every part of it must still
    reach the table's back edge."""
    geom = virtual_set.geometry("L", "")
    a = torso(400, 1000, 1350, 1880)
    for i, c in enumerate(range(1600, 1880)):         # the back of the body: the hem rises to 950
        a[1000 - int(50 * i / 280):1000, c] = 0
    s, tx, ty, _ = seat.fit(geom, [[[1550, 250, 120, 252]]], NO_TOUCH, [a])
    for c in range(1350, 1880, 10):
        bottom = np.nonzero(a[:, c])[0].max()
        assert s * bottom + ty >= seat.edge_y(geom["desk_back"], s * c + tx) - 1, c


def test_the_desk_line_ignores_raised_elbows_and_arms_on_the_desk():
    a = torso(400, 980, 600, 1200) > 0                  # the body, hidden behind the desk along a line
    for i, c in enumerate(range(600, 1200)):
        a[980 - int(40 * i / 600):980, c] = False         # the desk line slants: 980 on the left, 940 on the right
    a[980:1040, 800:950] = True                          # forearms and hands on the desk, in front
    a[700:1000, 1200:1260] = False
    a[400:760, 1200:1260] = True                         # a raised elbow on the right, ending in mid-air
    cols, line = seat.desk_line(a, np.arange(600, 1260))
    assert abs(float(line[cols == 600][0]) - 979) <= 3 and abs(float(line[cols == 1190][0]) - 941) <= 3
    assert not ((cols >= 1200) & (cols < 1260)).any() and not ((cols >= 800) & (cols < 950)).any()
