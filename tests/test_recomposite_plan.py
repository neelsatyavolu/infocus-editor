"""recomposite.py's decisions: how a take is split across workers, when a take fails, what a failure does to the
picture (never mix sets), the time guard, and reading frames by index."""
import sys
from pathlib import Path

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


def test_only_old_graphics_under_a_fresh_cover_are_cut():
    baked = [{"zone": "name bar (left)", "first": 100, "last": 300},
             {"zone": "Follow card", "first": 400, "last": 600},      # a logo bug the edit patched, not covered
             {"zone": "Follow card", "first": 700, "last": 900}]
    overlays = [{"id": "dual", "at": 98, "covers": [100, 300], "render": {"type": "dual"}},
                {"id": "bug", "at": 400, "covers": [400, 600], "file": "/job/show/Graphics/fixes/bug_patch.mov"},
                {"id": "follow", "at": 698, "covers": [700, 900], "file": "{PACKAGE}/08 Follow InFocus/x.mov"}]
    seg = {"in": 0, "out": 1000}
    got = recomposite.cut_windows(baked, overlays, seg, "/job/show")
    assert [(a, b) for a, b, _ in got] == [(100, 300), (700, 900)]


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
