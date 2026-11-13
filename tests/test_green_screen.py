"""Raw green-screen camera feeds: telling them from baked-set frames, which camera they are, the chroma-key alpha and
the spill. Synthetic frames only (no real green-screen sample yet)."""
import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import green_screen  # noqa: E402

H, W = 1080, 1920
KEY = (40, 175, 70)
SHIRT = (60, 70, 140)


def green_frame(people_x=(), rng_seed=0, h=H, w=W):
    """A lit green screen with a seated 'anchor' (a head over a torso running off the bottom) at each centre x."""
    rng = np.random.default_rng(rng_seed)
    f = np.clip(np.array(KEY, np.float32) + rng.normal(0, 4, (h, w, 3)), 0, 255).astype(np.uint8)
    for cx in people_x:
        x = int(cx * w)
        f[int(0.25 * h):int(0.45 * h), x - w // 40:x + w // 40] = (190, 150, 130)          # face
        f[int(0.45 * h):, x - w // 10:x + w // 10] = SHIRT                                   # body
    return f


def test_green_frames_are_told_from_the_sets():
    assert green_screen.is_green(green_frame([0.3, 0.7]))
    rng = np.random.default_rng(2)
    set_like = np.clip(rng.normal((110, 95, 80), 20, (H, W, 3)), 0, 255).astype(np.uint8)   # warm studio set
    assert not green_screen.is_green(set_like)
    dark_green = np.full((H, W, 3), (20, 45, 25), np.uint8)                                  # a dark green wall
    assert not green_screen.is_green(dark_green)


def test_take_needs_most_sampled_frames_green():
    g, s = green_frame([0.5]), np.full((H, W, 3), 120, np.uint8)
    assert green_screen.is_green_take([g, g, s])
    assert not green_screen.is_green_take([g, s, s])
    assert not green_screen.is_green_take([])


def test_background_share_ignores_the_anchors():
    f = green_frame([0.5])
    mask = np.zeros((H, W), bool)
    mask[:, 600:1320] = True                                    # an anchor filling the middle of the frame
    f[:, 600:1320] = SHIRT
    assert green_screen.green_share(f, mask) > 0.95


def test_camera_from_where_the_people_sit():
    assert green_screen.green_camera(green_frame([0.3, 0.7])) == "M"
    assert green_screen.green_camera(green_frame([0.78])) == "L"       # TV on the left, anchor on the right
    assert green_screen.green_camera(green_frame([0.25])) == "R"
    assert green_screen.green_camera(green_frame([])) is None


def test_camera_works_at_analysis_resolution_and_ignores_clutter():
    f = green_frame([0.78], h=270, w=480)
    f[:, :30] = (90, 90, 90)                                    # a flat beside the screen, floor to ceiling
    assert green_screen.green_camera(f) == "L"


def test_alpha_keeps_the_anchor_and_drops_the_key():
    f = green_frame([0.5])
    vis = np.zeros((H, W), np.float32)
    vis[int(0.22 * H):, 748:1172] = 1                           # Vision's matte, 20 px loose
    a = green_screen.green_alpha(f, vis)
    assert a[800, 960] > 0.98 and a[350, 960] > 0.98             # body and face
    assert a[800, 1165] < 0.05 and a[800, 755] < 0.05            # key inside Vision's loose edge
    assert a[800, 300] < 0.01                                    # key far away


def test_alpha_ignores_people_vision_didnt_keep():
    f = green_frame([0.25, 0.75])
    vis = np.zeros((H, W), np.float32)
    vis[int(0.24 * H):, 1240:1640] = 1                          # only the right-hand anchor is kept (e.g. L camera)
    a = green_screen.green_alpha(f, vis)
    assert a[800, 1440] > 0.98 and a[800, 480] < 0.01


def test_alpha_keeps_green_clothes_deep_inside_the_anchor():
    f = green_frame([0.5])
    f[700:1000, 900:1020] = KEY                                 # a green tie
    vis = np.zeros((H, W), np.float32)
    vis[int(0.24 * H):, 768:1152] = 1
    a = green_screen.green_alpha(f, vis)
    assert a[850, 960] > 0.9


def test_soft_edge_is_partial():
    f = green_frame([])
    f[:, 900:1000] = (0.5 * np.array(KEY) + 0.5 * np.array((200, 160, 140))).astype(np.uint8)   # half-hair, half-key
    vis = np.zeros((H, W), np.float32)
    vis[:, 940:1100] = 1                                        # the hair is at Vision's edge
    a = green_screen.green_alpha(f, vis)
    assert 0.1 < a[500, 950] < 0.9


def test_despill_pulls_green_out_of_edges_only():
    f = np.zeros((40, 40, 3), np.uint8)
    f[...] = (150, 190, 140)                                    # a white shirt with green spill
    a = np.zeros((40, 40), np.float32)
    a[:, :20] = 1
    a[:, 20] = 0.5
    out = green_screen.despill(f, a)
    assert out[20, 20, 1] == 145                                 # edge: green no higher than red and blue's mean
    assert out[20, 0, 1] == 190                                  # far from the edge: untouched
    assert f[20, 20, 1] == 190                                   # input untouched
    skin = np.full((40, 40, 3), (200, 150, 130), np.uint8)
    assert (green_screen.despill(skin, a) == skin).all()          # red above green: nothing to pull


def test_classify_reads_green_frames_as_their_camera_only_when_asked():
    import analyze
    mid = np.full((270, 480, 3), (110, 95, 80), np.uint8)                   # stand-in for the empty Middle set
    f = green_frame([0.3, 0.7], h=270, w=480)
    assert analyze.classify(f, mid) == "GREEN"                              # in a render: raw green is a failure
    assert analyze.classify(f, mid, green_as_camera=True) == "M"           # telling camera files apart
    assert analyze.classify(green_frame([0.2], h=270, w=480), mid, green_as_camera=True) == "R"
    assert analyze.classify(mid, mid) == "M"                                 # a baked-set frame is unchanged
