"""Side-set TVs left on the switcher's blue key (#0047BB) instead of the TV Standby: the classifier still sees the
L/R sets, the blue never reads as a filmed card, and the anchor foreground drops the blue at its edges.
Synthetic data only."""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import common  # noqa: E402
import foreground  # noqa: E402
import tv_key  # noqa: E402

BLUE = (0, 71, 187)
STANDBY = (15, 17, 15)


def _swatch(*colors):
    return np.array([colors], dtype=np.uint8)


def _studio(screen_cam=None, fill=BLUE, seed=3):
    """A 1080p studio frame (busy set, no dark areas) with one TV screen filled."""
    f = np.random.default_rng(seed).integers(60, 200, (1080, 1920, 3), dtype=np.uint8)
    if screen_cam:
        x, y, w, h = common.SCREENS[screen_cam]
        f[y:y + h, x:x + w] = fill
    return f


def _small(frame):
    return np.asarray(Image.fromarray(frame).resize((analyze.SW, analyze.SH), Image.BOX))


def _mid(tmp_path):
    png = tmp_path / "pkg" / common.THEMES[""]["middle"]
    png.parent.mkdir(parents=True)
    Image.fromarray(np.random.default_rng(11).integers(0, 90, (1080, 1920, 3), dtype=np.uint8)).save(png)   # a darker set
    return analyze.middle_set(str(tmp_path / "pkg"))


def test_the_key_blue_and_its_compressed_shades_count_but_standby_skin_and_navy_dont():
    key, soft, bright = BLUE, (9, 76, 179), (4, 66, 196)
    navy, denim, skin, white = (25, 35, 90), (70, 90, 130), (224, 172, 140), (236, 239, 234)
    f = _swatch(key, soft, bright, STANDBY, navy, denim, skin, white)
    assert tv_key.key_mask(f)[0].tolist() == [True, True, True, False, False, False, False, False]


def test_a_side_set_with_its_tv_on_the_blue_key_is_still_that_side(tmp_path):
    mid = _mid(tmp_path)
    assert analyze.classify(_small(_studio("L")), mid) == "L"
    assert analyze.classify(_small(_studio("R")), mid) == "R"


def test_a_side_set_on_standby_is_still_that_side(tmp_path):
    mid = _mid(tmp_path)
    assert analyze.classify(_small(_studio("L", STANDBY)), mid) == "L"
    assert analyze.classify(_small(_studio("R", STANDBY)), mid) == "R"


def test_the_blue_key_is_not_card_text_but_white_text_on_it_is():
    blue = _small(_studio("L"))
    assert analyze.card_text(blue, "L") < 0.002         # card_windows wants 0.006; was ~1.0 before the key
    texted = _studio("L")
    x, y, w, h = common.SCREENS["L"]
    texted[y + 60:y + 140, x + 80:x + 700] = (236, 239, 234)
    assert analyze.card_text(_small(texted), "L") > 0.006


def test_the_foreground_drops_the_blue_key_at_the_anchors_edge():
    rect = common.SCREENS["L"]
    x, y, w, h = rect
    f = _studio("L")
    matte = np.zeros(f.shape[:2], np.uint8)
    blue_px, skin_px = (y + 300, x + 300), (y + 300, x + 700)
    f[skin_px[0] - 3:skin_px[0] + 4, skin_px[1] - 3:skin_px[1] + 4] = (224, 172, 140)
    for py, px in (blue_px, skin_px):                 # a soft matte edge: half anchor, by Vision's guess
        matte[py - 3:py + 4, px - 3:px + 4] = 128
    a = foreground.alpha_for(f, matte, rect)
    assert a[blue_px] < 0.05                          # blue under a soft edge is screen, not anchor
    assert a[skin_px] > 0.3                           # skin under the same edge stays


def test_blue_spill_on_a_soft_edge_is_neutralised_and_skin_is_untouched():
    f = _swatch((20, 50, 103), (224, 172, 140)).repeat(2, 0)    # hair mixed with the key, then skin
    a = np.full(f.shape[:2], 0.5, np.float32)
    out = tv_key.despill(f, a)
    assert out[0, 0, 2] <= out[0, 0, 1]               # no channel pushes the edge toward blue
    assert out[0, 1].tolist() == [224, 172, 140]


def test_on_a_standby_show_a_blue_shirt_at_the_anchors_edge_is_not_keyed():
    rect = common.SCREENS["L"]
    x, y, w, h = rect
    f = _studio("L", STANDBY)
    matte = np.zeros(f.shape[:2], np.uint8)
    py, px = y + 300, x + 300
    f[py - 3:py + 4, px - 3:px + 4] = (30, 60, 180)  # a royal blue shirt crossing the TV
    matte[py - 3:py + 4, px - 3:px + 4] = 128
    assert not tv_key.on_key(f, rect)
    assert foreground.alpha_for(f, matte, rect)[py, px] > 0.3
    assert tv_key.on_key(_studio("L"), rect)


def test_a_blue_shirt_covering_most_of_a_standby_tv_does_not_turn_keying_on():
    rect = common.SCREENS["R"]
    x, y, w, h = rect
    f = _studio("R", STANDBY)
    matte = np.zeros(f.shape[:2], np.uint8)
    f[y:y + h, x:x + w * 2 // 3] = (30, 60, 180)     # the anchor's royal blue shirt over two thirds of the TV
    matte[y:y + h, x:x + w * 2 // 3] = 255
    assert not tv_key.on_key(f, rect, matte)


def test_over_the_key_a_half_anchor_edge_pixel_keeps_about_half_its_opacity():
    rect = common.SCREENS["L"]
    x, y, w, h = rect
    f = _studio("L")
    matte = np.zeros(f.shape[:2], np.uint8)
    py, px = y + 300, x + 300
    f[py - 3:py + 4, px - 3:px + 4] = (20, 50, 103)   # dark hair mixed half and half with the key
    matte[py - 3:py + 4, px - 3:px + 4] = 128
    assert 0.4 < foreground.alpha_for(f, matte, rect)[py, px] < 0.65
