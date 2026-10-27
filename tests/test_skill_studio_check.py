"""The studio-shot check (analyze.classify, verify rule 1) must never push the agent into changing the package
graphics or the encoder: a two-shot a little off the set image still reads as M, and the edit's own fresh graphics
are left out of the check. Synthetic data only."""
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import common  # noqa: E402
import verify_render  # noqa: E402

BIG_CARD = (48, 40, 1800, 1000)         # far bigger than the Follow card: most of the frame


def _package(tmp_path):
    mid = np.random.default_rng(5).integers(60, 200, (1080, 1920, 3), dtype=np.uint8)
    png = tmp_path / "pkg" / common.THEMES[""]["middle"]
    png.parent.mkdir(parents=True)
    Image.fromarray(mid).save(png)
    return str(tmp_path / "pkg"), mid


def _small(frame):
    return np.asarray(Image.fromarray(frame).resize((analyze.SW, analyze.SH), Image.BOX))


def _card_mask():
    x0, y0, x1, y1 = BIG_CARD
    m = np.zeros((analyze.SH, analyze.SW), bool)
    m[y0 // analyze.Q:y1 // analyze.Q, x0 // analyze.Q:x1 // analyze.Q] = True
    return m


def _with_card(frame):
    x0, y0, x1, y1 = BIG_CARD
    f = frame.copy()
    f[y0:y1, x0:x1] = (12, 14, 12)
    return f


def test_a_two_shot_lit_or_encoded_a_little_off_the_set_image_is_still_m(tmp_path):
    pkg, mid = _package(tmp_path)
    off = np.clip(mid.astype(np.int16) + 22, 0, 255).astype(np.uint8)   # 22 levels off everywhere (was over 18)
    assert analyze.classify(_small(off), analyze.middle_set(pkg)) == "M"


def test_the_edits_own_graphic_is_left_out_of_the_studio_check(tmp_path):
    pkg, mid = _package(tmp_path)
    f, m = _small(_with_card(mid)), analyze.middle_set(pkg)
    assert analyze.classify(f, m) != "M"                                  # the card hides most of the set
    assert analyze.classify(f, m, ignore=_card_mask()) == "M"


def _video(path, frames, rate, codec_args):
    h, w = frames[0].shape[:2]
    pix = "rgba" if frames[0].shape[2] == 4 else "rgb24"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", pix, "-s", f"{w}x{h}", "-r", rate,
                    "-i", "-", *codec_args, str(path)], input=b"".join(f.tobytes() for f in frames), check=True)


def test_rule_1_passes_a_two_shot_under_a_big_fresh_graphic(tmp_path):
    pkg, mid = _package(tmp_path)
    n = 6
    overlay = np.zeros((1080, 1920, 4), np.uint8)
    x0, y0, x1, y1 = BIG_CARD
    overlay[y0:y1, x0:x1] = (12, 14, 12, 255)
    _video(tmp_path / "card.mov", [overlay] * n, "30000/1001",
           ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"])
    _video(tmp_path / "render.mp4", [_with_card(mid)] * n, "60000/1001", ["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    edl = {"show": "Test Show", "fps": "60000/1001", "package": pkg, "source": "/unused.mov",
           "segments": [{"id": "close", "kind": "source", "in": 0, "out": n}],
           "overlays": [{"id": "follow", "at": 0, "file": str(tmp_path / "card.mov")}]}
    layout = {"segments": [{"id": "close", "kind": "source", "rec_in": 0, "rec_out": n}],
              "overlays": [{"id": "follow", "rec_in": 0, "rec_out": n}], "frames": n}
    cams = verify_render.render_cams(str(tmp_path / "render.mp4"), edl, layout, str(tmp_path))
    assert cams["close"] == ["M"] * n
    assert verify_render.rule_switcher_frames(cams, layout) == []


def test_a_full_frame_hold_stays_in_the_studio_check(tmp_path):
    pkg, mid = _package(tmp_path)
    opaque = np.dstack([mid, np.full(mid.shape[:2], 255, np.uint8)])
    _video(tmp_path / "hold.mov", [opaque] * 3, "60000/1001",
           ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"])
    edl = {"package": pkg, "overlays": [{"id": "hold", "at": 0, "file": str(tmp_path / "hold.mov")}]}
    layout = {"overlays": [{"id": "hold", "rec_in": 0, "rec_out": 3}]}
    assert verify_render.overlay_regions(edl, layout, str(tmp_path)) == []


def test_a_smaller_overlay_clip_is_masked_where_the_render_puts_it(tmp_path):
    pkg, _ = _package(tmp_path)
    half = np.zeros((540, 960, 4), np.uint8)
    half[20:120, 24:500] = 255                                   # drawn at half size; the render doubles it
    _video(tmp_path / "half.mov", [half] * 3, "30000/1001",
           ["-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"])
    edl = {"package": pkg, "overlays": [{"id": "follow", "at": 0, "file": str(tmp_path / "half.mov")}]}
    layout = {"overlays": [{"id": "follow", "rec_in": 0, "rec_out": 3}]}
    [(_, _, mask)] = verify_render.overlay_regions(edl, layout, str(tmp_path))
    ys, xs = np.nonzero(mask)
    assert xs.max() * analyze.Q >= 1000 - 4 and ys.max() * analyze.Q >= 240 - 4    # 1080p box (48,40)-(1000,240)
