"""recomposite.py's decisions: how a take is split across workers, when a take fails, what a failure does to the
picture (never mix sets), the time guard, and reading frames by index."""
import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import recomposite  # noqa: E402


def test_chunks_cover_every_frame_once():
    parts = recomposite.chunks(1000, 7)
    assert sum(n for _, n in parts) == 1000
    assert [f for f, _ in parts] == sorted(f for f, _ in parts)
    assert all(a + n == b for (a, n), (b, _) in zip(parts, parts[1:]))
    assert parts[0][0] == 0 and len(parts) == 7


def test_short_take_is_not_split_into_empty_chunks():
    assert recomposite.chunks(3, 8) == [(0, 1), (1, 1), (2, 1)]


def test_failed_take_falls_back_only_when_the_set_stays_the_same():
    assert recomposite.on_failure("", "") == "fallback"
    assert recomposite.on_failure(None, "") == "keep"        # filmed on a set the package doesn't have
    assert recomposite.on_failure("", "halloween") == "keep"
    assert recomposite.on_failure("halloween", "") == "keep"


def test_take_with_no_faces_fails():
    ok, reason = recomposite.take_ok(found=2, samples=6, areas=[1000] * 100, wanted=1.0)
    assert not ok and "faces" in reason


def test_take_wanting_an_impossible_scale_fails():
    ok, reason = recomposite.take_ok(found=6, samples=6, areas=[1000] * 100, wanted=1.6)
    assert not ok and "scale" in reason


def test_take_with_a_jumping_matte_fails():
    areas = [1000] * 90 + [400] * 10                        # 10% of frames lose more than a quarter of the anchor
    ok, reason = recomposite.take_ok(found=6, samples=6, areas=areas, wanted=1.0)
    assert not ok and "matte" in reason


def test_steady_take_passes():
    areas = [1000] * 97 + [600] * 3                         # a few frames off is fine
    assert recomposite.take_ok(found=5, samples=6, areas=areas, wanted=1.1) == (True, "")


def test_budget_projection():
    assert recomposite.over_budget(100, 1000, 30000)       # 3000 s projected
    assert not recomposite.over_budget(10, 1000, 30000)    # 300 s
    assert not recomposite.over_budget(5, 0, 30000)        # nothing measured yet


def test_a_short_flash_with_no_anchor_is_held_not_failed():
    areas = [0] * 28 + [1000] * 500                         # the switcher flashed a full-screen card at the start
    assert recomposite.take_ok(found=6, samples=6, areas=areas, wanted=1.0) == (True, "")


def test_a_take_mostly_without_an_anchor_fails():
    ok, reason = recomposite.take_ok(found=6, samples=6, areas=[0] * 60 + [1000] * 400, wanted=1.0)
    assert not ok and "no anchor" in reason


def test_frames_without_an_anchor_show_the_nearest_frame_with_one():
    assert recomposite.hold_map([0, 0, 5, 6, 0, 7, 0]) == [2, 2, 2, 3, 3, 5, 5]
    assert recomposite.hold_map([0, 0]) == [0, 1]           # nothing to hold: frames stay as they are


def test_frame_reader_starts_exactly_on_the_frame_asked_for(tmp_path):
    import subprocess
    import numpy as np
    clip = tmp_path / "coded.mp4"                           # frame n is grey level 2n, with B-frames like a recording
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=1920x1080:r=60000/1001:d=3",
                    "-vf", "geq=lum='2*N':cb=128:cr=128", "-c:v", "libx264", "-qp", "0", "-bf", "2", "-g", "48",
                    str(clip)], check=True)
    out = subprocess.run(recomposite.reader_args(str(clip), 100, 5, 60000 / 1001, "gray"), capture_output=True,
                         check=True).stdout
    exact = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-vf", "select=between(n\\,100\\,104)",
                            "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                           capture_output=True, check=True).stdout
    level = lambda b: [round(float(f.mean())) for f in np.frombuffer(b, np.uint8).reshape(-1, 1080, 1920)]  # noqa: E731
    assert len(level(exact)) == 5 and level(out) == level(exact)


SHOW_1021 = Path.home() / "InFocusEditor/fixtures/2026-10-21/InFocus Show 10-21-2026.mov"


@pytest.mark.fixtures
@pytest.mark.skipif(not SHOW_1021.exists(), reason="needs the 10-21 show fixture")
def test_frame_reader_is_exact_on_a_real_recording():
    """The 10-21 recording flashes a full-screen card on frames 6080-6107. Reading from 6079 without resetting the
    timestamps once returned 40 frames of card: the raw output repeated its first frame over the trimmed second."""
    import subprocess
    import numpy as np
    out = subprocess.run(recomposite.reader_args(str(SHOW_1021), 6079, 32, 60000 / 1001, "gray"),
                         capture_output=True, check=True).stdout
    dark = [float(f.mean()) < 40 for f in np.frombuffer(out, np.uint8).reshape(-1, 1080, 1920)]
    assert dark == [True] * 29 + [False] * 3


def test_every_old_graphic_in_a_take_is_filled_whether_or_not_a_fix_up_covers_it():
    baked = [{"zone": "name bar (left)", "first": 100, "last": 300},
             {"zone": "Follow card", "first": 400, "last": 600},      # a logo bug the edit patched, not covered
             {"zone": "Follow card", "first": 700, "last": 900}]
    overlays = [{"id": "dual", "at": 98, "covers": [100, 300], "render": {"type": "dual"}},
                {"id": "bug", "at": 400, "covers": [400, 600], "file": "/job/show/Graphics/fixes/bug_patch.mov"},
                {"id": "follow", "at": 698, "covers": [700, 900], "file": "{PACKAGE}/08 Follow InFocus/x.mov"}]
    seg = {"in": 0, "out": 1000}
    got = recomposite.cut_windows(baked, overlays, seg, "/job/show")
    assert [(a, b) for a, b, _ in got] == [(100, 300), (400, 600), (700, 900)]


def test_a_take_on_a_set_the_package_doesnt_have_fails():
    ok, reason = recomposite.take_ok(found=6, samples=6, areas=[1000] * 100, wanted=1.0, known_set=False)
    assert not ok and "set the package doesn't have" in reason


def test_cuts_follow_the_fresh_cover_frame_by_frame():
    """A 29.97 cover over a 59.94 show: each cover frame lasts two show frames, and while it animates in, only what
    it has drawn so far is cut from the anchor."""
    boxes = [None, (0, 0, 10, 10), (0, 0, 20, 20), (0, 0, 30, 30)]
    window = {"first": 99, "last": 106}
    got = recomposite.per_frame_cuts(window, at=98, boxes=boxes, clip_rate=30000 / 1001, show_rate=60000 / 1001)
    assert got == [(100, 100, (0, 0, 10, 10)), (101, 101, (0, 0, 10, 10)), (102, 102, (0, 0, 20, 20)),
                   (103, 103, (0, 0, 20, 20)), (104, 104, (0, 0, 30, 30)), (105, 105, (0, 0, 30, 30))]


def test_old_graphic_frames_borrow_the_nearest_clean_frame():
    cuts = [(t, t, (0, 0, 10, 10)) for t in range(120, 130)] + [(t, t, (5, 5, 20, 20)) for t in range(190, 200)]
    got = recomposite.patch_runs(cuts, take_in=100, take_out=200)
    assert got == [(120, 129, 119), (190, 199, 189)]              # the frame just before each, the nearest clean one


def test_a_graphic_on_the_takes_first_frames_borrows_the_frame_after_it():
    cuts = [(t, t, (0, 0, 10, 10)) for t in range(100, 105)]
    assert recomposite.patch_runs(cuts, take_in=100, take_out=200) == [(100, 104, 105)]


def test_a_graphic_over_the_whole_take_has_nothing_to_borrow():
    cuts = [(t, t, (0, 0, 10, 10)) for t in range(100, 200)]
    assert recomposite.patch_runs(cuts, take_in=100, take_out=200) == [(100, 199, None)]


def test_plan_entries_carry_what_the_take_was_made_from():
    edl = {"source": "/rec.mov", "segments": [], "overlays": []}
    seg = {"id": "open", "kind": "source", "cam": "M", "in": 10, "out": 90}
    e = {"id": "open", "recording": "r1", "cam": "M", "recorded_theme": "", "s": 1.0, "tx": 0.0, "ty": 0.0,
         "status": "recomposited", "flag": None, "note": "", "tight": False}
    assert recomposite.plan_entry(e, "/show", edl, seg)["source"]["in"] == 10


def test_every_old_graphic_is_filled_and_matched_to_the_cover_over_it():
    baked = [{"zone": "name bar (left)", "first": 100, "last": 110},
             {"zone": "Follow card", "first": 100, "last": 110},
             {"zone": "name bar (right)", "first": 300, "last": 310}]       # nothing covers this one
    overlays = [{"id": "follow", "at": 98, "covers": [100, 110], "file": "{PACKAGE}/f.mov"},
                {"id": "dual", "at": 98, "covers": [100, 110], "render": {"type": "dual"}}]
    drawn = {"follow": [(48, 40, 700, 300)] * 20, "dual": [(150, 840, 1770, 975)] * 20}
    got = recomposite.cut_windows(baked, overlays, {"in": 0, "out": 400}, "/job/show",
                                  cover_boxes=lambda o: (drawn[o["id"]], 60000 / 1001))
    by_box = {box for _, _, box in got if _ == 105}
    assert by_box == {(150, 840, 1770, 975), (48, 40, 700, 300)}            # each zone gets its own cover's box
    assert (300, 310) in {(a, b) for a, b, _ in got}                        # the uncovered one is still filled


def test_a_finished_pass_is_never_abandoned():
    assert recomposite.over_budget(5000, 30000, 30000) is False
    assert recomposite.over_budget(100, 1000, 30000) is True


def test_a_re_rendered_take_adopts_its_cameras_transform():
    siblings = [{"s": 0.95, "tx": 20.0, "ty": 10.0}, {"s": 0.95, "tx": 20.0, "ty": 10.0}]
    near = recomposite.adopt({"s": 0.96, "tx": 26.0, "ty": 4.0}, siblings)
    assert (near["s"], near["tx"], near["ty"]) == (0.95, 20.0, 10.0)
    far = recomposite.adopt({"s": 0.80, "tx": 200.0, "ty": 4.0}, siblings)    # the camera moved: keep its own
    assert (far["s"], far["tx"]) == (0.80, 200.0)


def test_elbows_are_matched_to_each_anchor_by_their_shoulders():
    boxes = [[[530, 300, 110, 215], [1310, 300, 110, 215]]] * 4
    left = {"ls": [480, 450, 0.8], "rs": [700, 450, 0.8], "le": [460, 850, 0.8], "re": [720, 870, 0.8]}
    right = {"ls": [1260, 450, 0.8], "rs": [1480, 450, 0.8], "le": [1240, 900, 0.8], "re": [1500, 900, 0.8]}
    got = recomposite.elbows([[left, right]] * 4, boxes, 2)
    assert [round(y) for _, y, _, _ in got] == [860, 900]
    assert [tuple(round(v) for v in span) for _, _, _, span in got] == [(480, 700), (1260, 1480)]


def test_without_elbows_on_most_samples_there_is_no_pose_seat():
    boxes = [[[530, 300, 110, 215]]] * 4
    arm = {"ls": [480, 450, 0.8], "rs": [700, 450, 0.8], "le": [460, 850, 0.1]}   # elbow too unsure
    assert recomposite.elbows([[arm]] * 4, boxes, 1) is None


def test_samples_skip_frames_with_an_old_graphic_on_screen():
    frames = recomposite.sample_frames(1000, 1600, [(1050, 1400)], 6)
    assert len(frames) == 6 and not any(1050 <= f <= 1400 for f in frames)
    assert recomposite.sample_frames(1000, 1600, [(900, 1700)], 6) == [1050, 1150, 1250, 1350, 1450, 1550]


def test_a_sliver_of_clean_frames_is_not_where_the_take_is_measured():
    frames = recomposite.sample_frames(6080, 6612, [(6134, 6720)], 6)        # only 54 clean frames, a flash among them
    assert max(frames) > 6200


def test_samples_without_anyone_in_them_are_dropped():
    full = np.zeros((1080, 1920), np.float32)
    full[300:900, 500:1400] = 1
    assert recomposite.usable([full, np.zeros_like(full), full, full]) == [0, 2, 3]
    assert recomposite.usable([np.zeros_like(full)] * 4) == [0, 1, 2, 3]   # nobody anywhere: keep them all


def test_the_agents_seat_replaces_the_scripts_guess():
    seg = {"id": "l", "seat": {"scale": 0.95, "x": 300, "y": 80}}
    entries = [{"id": "l", "recording": "r1", "cam": "L", "s": 0.94, "tx": 295.0, "ty": 62.0, "found": 6},
               {"id": "r", "recording": "r1", "cam": "R", "s": 1.0, "tx": 0.0, "ty": 0.0, "found": 6}]
    out = recomposite.apply_seats(entries, {"l": seg, "r": {"id": "r"}})
    assert (out[0]["s"], out[0]["tx"], out[0]["ty"], out[0]["seated_by"]) == (0.95, 300.0, 80.0, "agent")
    assert (out[1]["s"], out[1]["tx"], out[1]["ty"]) == (1.0, 0.0, 0.0) and out[1].get("seated_by") != "agent"
    assert entries[0]["tx"] == 295.0                             # inputs untouched


def test_an_agent_seat_is_judged_by_its_own_scale():
    e = recomposite.apply_seats([{"id": "l", "s": 1.4, "tx": 0.0, "ty": 0.0, "wanted": 1.4}],
                                {"l": {"id": "l", "seat": {"scale": 1.0, "x": 0, "y": 0}}})[0]
    assert e["wanted"] == 1.0
