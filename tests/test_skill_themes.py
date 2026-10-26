"""Seasonal themes: the theme switch, the camera classifier on a seasonal set, the theme's accent color and the
theme's intro and outro in verify rule 5. Synthetic data only."""
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import common  # noqa: E402
import verify_render  # noqa: E402

HALLOWEEN = "{PACKAGE}/Themes/Halloween/03 Intro & Outro/"


@pytest.fixture(autouse=True)
def no_theme_env(monkeypatch):
    monkeypatch.delenv("INFOCUS_THEME", raising=False)


def test_theme_comes_from_the_edl_then_the_environment(monkeypatch):
    assert common.theme_name({}) == ""
    assert common.theme_name({"theme": "Halloween"}) == "halloween"
    monkeypatch.setenv("INFOCUS_THEME", "halloween")
    assert common.theme_name({}) == "halloween"
    assert common.theme_name({"theme": "halloween"}) == "halloween"
    assert common.theme_name({"theme": ""}) == ""       # an explicit regular look beats the environment


def test_an_unknown_theme_is_refused():
    with pytest.raises(RuntimeError, match="Unknown theme"):
        common.theme_name({"theme": "christmas"})


def test_theme_files_live_under_themes_in_the_package():
    edl = {"package": "/pkg", "theme": "halloween"}
    assert common.theme_file("intro", edl) == "/pkg/Themes/Halloween/03 Intro & Outro/InFocus Intro Halloween 2026.mp4"
    assert common.theme_file("follow", {"package": "/pkg"}) == "/pkg/08 Follow InFocus/InFocus Follow Us.mov"
    assert set(common.THEMES["halloween"]) == set(common.THEMES[""])


def _package(tmp_path, *themes):
    rng = np.random.default_rng(7)
    sets = {}
    for theme in themes:
        png = tmp_path / "pkg" / common.THEMES[theme]["middle"]
        png.parent.mkdir(parents=True, exist_ok=True)
        sets[theme] = rng.integers(60, 200, (1080, 1920, 3), dtype=np.uint8)
        Image.fromarray(sets[theme]).save(png)
    return str(tmp_path / "pkg"), sets


def _small(frame):
    return np.asarray(Image.fromarray(frame).resize((analyze.SW, analyze.SH), Image.BOX))


def test_a_two_shot_on_either_installed_middle_set_is_m(tmp_path):
    pkg, sets = _package(tmp_path, "", "halloween")
    mid = analyze.middle_set(pkg)
    assert mid.shape[0] == 2
    assert analyze.classify(_small(sets[""]), mid) == "M"
    assert analyze.classify(_small(sets["halloween"]), mid) == "M"


def test_without_the_seasonal_set_only_the_regular_two_shot_is_m(tmp_path):
    pkg, sets = _package(tmp_path, "")
    other = np.random.default_rng(9).integers(60, 200, (1080, 1920, 3), dtype=np.uint8)
    mid = analyze.middle_set(pkg)
    assert mid.shape[0] == 1
    assert analyze.classify(_small(sets[""]), mid) == "M"
    assert analyze.classify(_small(other), mid) != "M"


def test_a_package_without_a_middle_set_says_so(tmp_path):
    with pytest.raises(RuntimeError, match="No Middle set"):
        analyze.middle_set(str(tmp_path))


def _swatch(*colors):
    return np.array([colors], dtype=np.uint8)


def test_green_and_pumpkin_graphics_count_as_overlays_but_skin_and_oak_dont():
    pumpkin, deep, green = (255, 122, 26), (180, 68, 10), (11, 110, 62)
    skin_light, skin_dark, oak = (224, 172, 140), (141, 85, 36), (200, 150, 110)
    f = _swatch(pumpkin, deep, green, skin_light, skin_dark, oak)
    assert analyze.brand_mask(f)[0].tolist() == [True, True, True, False, False, False]


def test_rule_5_wants_the_themes_intro_and_outro():
    segs = [{"id": "intro", "kind": "file", "path": HALLOWEEN + "InFocus Intro Halloween 2026.mp4"},
            {"id": "open", "kind": "source", "in": 0, "out": 100},
            {"id": "outro", "kind": "file", "path": HALLOWEEN + "InFocus Outro Halloween 2026.mp4"}]
    assert verify_render.rule_intro_outro({"theme": "halloween", "segments": segs}) == []
    fails = verify_render.rule_intro_outro({"segments": segs})
    assert {f["segment"] for f in fails} == {"intro", "outro"}
