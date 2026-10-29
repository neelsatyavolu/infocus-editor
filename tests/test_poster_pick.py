"""poster_pick.py: the poster comes from the dual third's fully-on window, at the frame where every anchor's face
looks most natural (eyes open, mouth at rest, facing the camera). Pure functions only."""
import math
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import poster_pick  # noqa: E402


def face(eyes=0.3, mouth=0.02, yaw=0.0):
    return {"box": [0, 0, 100, 100], "eyes": eyes, "mouth": mouth, "yaw": yaw}


def test_full_window_skips_the_animation():
    grow = [9000 * i for i in range(1, 10)]                         # in-animation: the plate fills in
    counts = [0] + grow + [90000] * 50 + list(reversed(grow)) + [0]
    assert poster_pick.full_window(counts) == (10, 59)


def test_full_window_of_an_empty_clip_is_none():
    assert poster_pick.full_window([0, 0]) is None


def test_a_slightly_open_mouth_scores_below_a_closed_one():
    assert poster_pick.frame_score([face(mouth=0.011), face(mouth=0.015)]) > \
        poster_pick.frame_score([face(mouth=0.021), face(mouth=0.036)])


def test_blink_and_open_mouth_score_lower():
    good = poster_pick.frame_score([face(), face()])
    assert good > poster_pick.frame_score([face(), face(eyes=0.05)])
    assert good > poster_pick.frame_score([face(mouth=0.12), face()])
    assert good > poster_pick.frame_score([face(yaw=0.6), face()])


def test_missing_face_is_never_picked():
    assert poster_pick.frame_score([face()]) == -math.inf
    assert poster_pick.frame_score([face(), face(eyes=-1)]) < poster_pick.frame_score([face(), face(eyes=0.1)])


def test_candidates_are_spread_out():
    scores = {f: 1.0 - abs(f - 50) / 100 for f in range(0, 300, 5)}    # best at 50, smooth around it
    got = poster_pick.candidates(scores, k=4, min_gap=30)
    assert got[0] == 50 and len(got) == 4
    assert all(abs(a - b) >= 30 for i, a in enumerate(got) for b in got[i + 1:])


def test_candidates_leave_out_frames_with_a_missing_face():
    assert poster_pick.candidates({0: -math.inf, 30: 0.5}, k=6) == [30]
