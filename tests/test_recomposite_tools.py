"""The Vision helpers re-compositing and the poster use: they compile on first use and speak the agreed formats.
A blank picture has no faces and no people."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("swiftc"), reason="needs swiftc (macOS)")


@pytest.fixture(scope="module")
def blank(tmp_path_factory):
    d = tmp_path_factory.mktemp("tools")
    Image.fromarray(np.zeros((1080, 1920, 3), np.uint8)).save(d / "black.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=1920x1080:r=60000/1001:d=0.5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "black.mov")], check=True)
    return d


@pytest.mark.parametrize("tool", ["face_boxes", "face_marks"])
def test_face_tools_report_no_faces_on_a_blank_frame(blank, tool):
    out = subprocess.run([common.ensure_tool(tool), str(blank / "black.png")], capture_output=True, text=True,
                         check=True).stdout
    line = json.loads(out.splitlines()[0])
    assert line["path"] == str(blank / "black.png") and line["faces"] == []


def test_anchor_matte_writes_exactly_the_frames_asked_for(blank):
    out = subprocess.run([common.ensure_tool("anchor_matte"), str(blank / "black.mov"), "5", "10", "2"],
                         capture_output=True, check=True).stdout
    assert len(out) == 10 * 1920 * 1080 and max(out) == 0


def test_anchor_matte_stops_at_the_end_of_the_file(blank):
    out = subprocess.run([common.ensure_tool("anchor_matte"), str(blank / "black.mov"), "25", "100", "1"],
                         capture_output=True, check=True).stdout
    assert len(out) == 5 * 1920 * 1080                 # 30 frames in the clip, 25..29 exist
