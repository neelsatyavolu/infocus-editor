"""Virtual sets for re-compositing: each theme's Left/Middle/Right set, the table geometry and which set a recording
was filmed on. Synthetic data only."""
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
import virtual_set  # noqa: E402

REGULAR, HALLOWEEN = (120, 120, 120), (200, 100, 30)


@pytest.fixture(autouse=True)
def no_theme_env(monkeypatch):
    monkeypatch.delenv("INFOCUS_THEME", raising=False)


def solid(rgb):
    return np.full((common.H, common.W, 3), rgb, np.uint8)


def install_sets(pkg, theme, rgb):
    for key in ("left", "middle", "right"):
        path = pkg / common.THEMES[theme][key]
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(solid(rgb)).save(path)


def test_theme_sets_live_in_the_package():
    assert common.theme_file("left", {"package": "/pkg"}).endswith(
        "04 Virtual Sets/InFocus Virtual Set 2026 - Left.png")
    assert common.theme_file("right", {"package": "/pkg", "theme": "halloween"}).endswith(
        "Themes/Halloween/04 Virtual Sets/InFocus Virtual Set Halloween 2026 - Right.png")


def test_set_image_reads_the_cams_png(tmp_path):
    install_sets(tmp_path, "", REGULAR)
    img = virtual_set.set_image("L", "", str(tmp_path))
    assert img.shape == (common.H, common.W, 3) and tuple(img[500, 500]) == REGULAR


def test_missing_set_png_is_a_clear_error(tmp_path):
    install_sets(tmp_path, "", REGULAR)
    with pytest.raises(RuntimeError, match="No L set for theme 'halloween'"):
        virtual_set.set_image("L", "halloween", str(tmp_path))


def test_installed_themes_need_all_three_sets(tmp_path):
    install_sets(tmp_path, "", REGULAR)
    install_sets(tmp_path, "halloween", HALLOWEEN)
    (tmp_path / common.THEMES["halloween"]["right"]).unlink()
    assert virtual_set.installed_themes(str(tmp_path)) == [""]


def test_recorded_theme_picks_the_matching_set(tmp_path):
    install_sets(tmp_path, "", REGULAR)
    install_sets(tmp_path, "halloween", HALLOWEEN)
    frame = solid(HALLOWEEN)
    mask = np.zeros((common.H, common.W), bool)
    frame[300:900, 700:1100] = (30, 30, 30)
    mask[300:900, 700:1100] = True
    assert virtual_set.recorded_theme(frame, mask, "M", str(tmp_path)) == "halloween"


def test_unknown_themes_geometry_falls_back_to_regular():
    assert virtual_set.geometry("M", "halloween") == virtual_set.geometry("M", "")
    g = virtual_set.geometry("R", "")
    assert set(g) == {"seats", "desk_back", "desk_lip"} and g["seats"]["x"] == [490]


def test_recorded_theme_is_none_when_no_set_matches(tmp_path):
    install_sets(tmp_path, "", REGULAR)
    install_sets(tmp_path, "halloween", HALLOWEEN)
    frame = solid((30, 160, 60))                     # an older set the package doesn't have
    mask = np.zeros((common.H, common.W), bool)
    assert virtual_set.recorded_theme(frame, mask, "L", str(tmp_path)) is None


def test_the_tv_screen_is_left_out_of_the_match(tmp_path):
    install_sets(tmp_path, "", REGULAR)
    frame = solid(REGULAR)
    x, y, w, h = common.SCREENS["R"]
    frame[y:y + h, x:x + w] = (15, 17, 15)           # a card on the TV
    mask = np.ones((common.H, common.W), bool)       # the anchor covers everything but the TV and the table
    mask[y:y + h, x:x + w] = False
    mask[900:] = False
    assert virtual_set.recorded_theme(frame, mask, "R", str(tmp_path)) == ""
