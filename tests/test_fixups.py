"""Fix-up clips (scripts/fixups.py, scripts/poster.py) on small synthetic clips made with ffmpeg."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import fixups  # noqa: E402

RATE = "60000/1001"


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def frames_of(path, pix_fmt="rgba"):
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                                      "stream=width,height", "-of", "json", path],
                                     capture_output=True, text=True, check=True).stdout)["streams"][0]
    w, h = info["width"], info["height"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"],
                         capture_output=True, check=True).stdout
    ch = 4 if pix_fmt == "rgba" else 3
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, ch)


@pytest.fixture(scope="module")
def numbered(tmp_path_factory):
    """4 s at 59.94 where frame n is coded in colour: red = (n % 16) * 16, green = (n // 16) * 16.
    H.264 with a 120-frame GOP, so a time seek can land between keyframes like a real recording."""
    out = tmp_path_factory.mktemp("clips") / "numbered.mp4"
    ffmpeg("-f", "lavfi", "-i", f"color=black:s=1920x1080:r={RATE}:d=4",
           "-vf", "geq=r='mod(N\\,16)*16':g='floor(N/16)*16':b=128", "-c:v", "libx264", "-qp", "0",
           "-pix_fmt", "yuv444p", "-g", "120", str(out))
    return str(out)


def decode_number(rgb):
    r, g = rgb[540, 960, 0], rgb[540, 960, 1]
    return int(round(r / 16)) + 16 * int(round(g / 16))


@pytest.mark.parametrize("frame", [0, 37, 100, 181, 239])
def test_grab_returns_the_exact_indexed_frame(numbered, tmp_path, frame):
    out = fixups.grab(numbered, frame, str(tmp_path / "f.png"))
    assert decode_number(np.array(Image.open(out).convert("RGB"))) == frame


def test_hold_has_the_requested_frame_count(numbered, tmp_path):
    out = fixups.hold(numbered, 181, 29, str(tmp_path))
    assert Path(out).name == "hold_f181_29f.mov"
    frames = frames_of(out)
    assert len(frames) == 29
    assert all(decode_number(f[..., :3]) == 181 for f in frames[::7])
    assert (frames[..., 3] == 255).all()


def test_patch_alpha_is_zero_outside_the_rect(numbered, tmp_path):
    out = fixups.patch(numbered, 100, (28, 22, 182, 172), 10, str(tmp_path))
    assert Path(out).name == "patch_f100_28-22-182-172_10f.mov"
    frames = frames_of(out)
    assert len(frames) == 10
    a = frames[0][..., 3]
    inside = np.zeros_like(a, bool)
    inside[22:172, 28:182] = True
    assert (a[~inside] == 0).all()
    assert frames[0][~inside][:, :3].max() <= 4              # transparent pixels zeroed (± YUV rounding)
    assert (a[22 + fixups.FEATHER:172 - fixups.FEATHER, 28 + fixups.FEATHER:182 - fixups.FEATHER] >= 250).all()
    assert decode_number(frames[0][100:101, 100:101].repeat(1080, 0).repeat(1920, 1)) == 100


def test_under_keeps_the_overlays_alpha_on_top(numbered, tmp_path):
    overlay = tmp_path / "Follow Us.mov"
    # a 30-frame 29.97 overlay: an opaque white box at 50..250 x 50..150, transparent elsewhere
    box = np.zeros((1080, 1920, 4), np.uint8)
    box[50:150, 50:250] = 255
    Image.fromarray(box, "RGBA").save(tmp_path / "box.png")
    ffmpeg("-loop", "1", "-framerate", "30000/1001", "-i", str(tmp_path / "box.png"),
           "-frames:v", "30", *fixups.PRORES, str(overlay))
    out = fixups.under(str(overlay), numbered, 100, (0, 0, 400, 300), str(tmp_path / "out"))
    assert Path(out).name == "under_Follow_Us_f100_30f.mov"
    frames = frames_of(out)
    assert len(frames) == 30
    f = frames[5]
    assert (f[100, 150, :3] > 245).all() and f[100, 150, 3] == 255     # overlay on top
    assert f[250, 300, 3] == 255 and decode_number(f[250:251, 300:301].repeat(1080, 0).repeat(1920, 1)) == 100
    assert f[500, 900, 3] == 0                                         # outside both: transparent


def test_cli_rejects_a_rect_outside_the_frame(numbered, tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPTS / "fixups.py"), "patch", numbered, "10", "0,0,2000,10", "5",
                        str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 1 and "inside 1920x1080" in r.stderr


def test_poster_is_1280x720(numbered, tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    import poster
    out = poster.poster(numbered, 1.0, str(tmp_path / "p.jpg"))
    assert Image.open(out).size == (1280, 720)
