"""build_ffmpeg.py: the show built and rendered by ffmpeg alone, frame-exact, with layout.json and an FCPXML."""
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import build_ffmpeg  # noqa: E402
import common  # noqa: E402
import fixups  # noqa: E402

RATE = "60000/1001"
STEP = 32


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def number(rgb, x=960, y=540):
    r, g, b = (int(round(v / STEP)) for v in rgb[y, x, :3])
    return r + 8 * g + 64 * b


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("show")
    # source: 10 s at 59.94 where frame n is colour-coded (r, g, b) = base-8 digits of n, times 32
    ffmpeg("-f", "lavfi", "-i", f"color=black:s=1920x1080:r={RATE}:d=10",
           "-vf", "geq=r='mod(N\\,8)*32':g='mod(floor(N/8)\\,8)*32':b='floor(N/64)*32'",
           "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p", "-g", "120", str(d / "source.mp4"))
    # a 2 s 29.97 package (solid blue) and a 1 s 29.97 overlay: an opaque white box, transparent elsewhere
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=1920x1080:r=30000/1001:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           str(d / "pkg.mp4"))
    box = np.zeros((1080, 1920, 4), np.uint8)
    box[100:200, 100:400] = 255
    Image.fromarray(box, "RGBA").save(d / "box.png")
    ffmpeg("-loop", "1", "-framerate", "30000/1001", "-i", str(d / "box.png"), "-frames:v", "30", *fixups.PRORES,
           str(d / "box.mov"))
    edl = {"show": "Test Show", "fps": RATE, "source": str(d / "source.mp4"),
           "segments": [{"id": "a", "kind": "source", "in": 100, "out": 250},
                        {"id": "pkg", "kind": "file", "path": str(d / "pkg.mp4"), "audio": "match"},
                        {"id": "b", "kind": "source", "in": 400, "out": 460}],
           "overlays": [{"id": "box", "at": 120, "covers": [122, 170], "file": str(d / "box.mov")},
                        {"id": "edge", "at": 400, "file": str(d / "box.mov")}]}       # starts on a take's first frame
    (d / "edl.json").write_text(json.dumps(edl))
    (d / "Audio").mkdir()
    for seg, sec in (("a", 2.6), ("pkg", 2.1), ("b", 1.1)):
        t = np.arange(int(sec * 48000)) / 48000
        x = 0.5 * np.sin(2 * np.pi * 440 * t)
        sf.write(d / "Audio" / f"{seg}.wav", np.stack([x, x], 1).astype(np.float32), 48000)
    out = d / "out" / "Test Show v1.mp4"
    result = build_ffmpeg.build(str(d), str(out), version=1)
    frames = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                            capture_output=True, check=True).stdout
    return d, out, result, np.frombuffer(frames, np.uint8).reshape(-1, 1080, 1920, 3)


def rebuilt(built, tmp_path, plan, clip=True):
    """The same show with recomposite on and a Recomposite/plan.json; take "a" re-composited to solid magenta."""
    d0 = built[0]
    d = tmp_path / "show"
    shutil.copytree(d0 / "Audio", d / "Audio")
    edl = {**json.loads((d0 / "edl.json").read_text()), "recomposite": True}
    (d / "edl.json").write_text(json.dumps(edl))
    (d / "Recomposite").mkdir()
    sigs = {s["id"]: common.take_signature(str(d), edl, s) for s in edl["segments"] if s["kind"] == "source"}
    plan = {**plan, "takes": [{**t, "source": sigs[t["id"]]} for t in plan["takes"]]}
    (d / "Recomposite" / "plan.json").write_text(json.dumps(plan))
    if clip:
        ffmpeg("-f", "lavfi", "-i", f"color=c=magenta:s=1920x1080:r={RATE}", "-frames:v", "150", "-c:v", "libx264",
               "-pix_fmt", "yuv420p", str(d / "Recomposite" / "a - Recomposite.mov"))
    out = d / "out.mp4"
    build_ffmpeg.build(str(d), str(out), version=1)
    frames = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                            capture_output=True, check=True).stdout
    return d, np.frombuffer(frames, np.uint8).reshape(-1, 1080, 1920, 3)


PLAN_A = {"theme": "", "abandoned": None, "takes": [{"id": "a", "status": "recomposited"},
                                                    {"id": "b", "status": "fallback"}]}


def magenta(frame):
    r, g, b = (int(v) for v in frame[540, 960])
    return r > 200 and b > 200 and g < 60


def test_recomposited_take_reads_its_clip(built, tmp_path):
    _, frames = rebuilt(built, tmp_path, PLAN_A)
    assert len(frames) == 330
    assert magenta(frames[0]) and magenta(frames[149])
    assert bool((frames[20][150, 250] > 230).all())                # overlays still go on top
    assert number(frames[270]) == 400                              # a fallback take reads the recording


def test_layout_marks_recomposited_takes(built, tmp_path):
    d, _ = rebuilt(built, tmp_path, PLAN_A)
    layout = json.loads((d / "layout.json").read_text())
    assert [s.get("recomposite", False) for s in layout["segments"]] == [True, False, False]


def test_abandoned_plan_builds_as_today(built, tmp_path):
    _, frames = rebuilt(built, tmp_path, {**PLAN_A, "abandoned": "too slow"})
    assert number(frames[0]) == 100 and number(frames[149]) == 249


def test_a_take_changed_since_recompositing_is_refused(built, tmp_path):
    d, _ = rebuilt(built, tmp_path, PLAN_A)
    edl = json.loads((d / "edl.json").read_text())
    edl["segments"][0]["in"] += 30                                 # a fix moved the take
    (d / "edl.json").write_text(json.dumps(edl))
    with pytest.raises(RuntimeError, match="recomposite.py --only a"):
        build_ffmpeg.build(str(d), str(d / "out2.mp4"), version=2)


def test_missing_clip_is_a_clear_error(built, tmp_path):
    with pytest.raises(RuntimeError, match="a - Recomposite.mov"):
        rebuilt(built, tmp_path, PLAN_A, clip=False)


def test_renders_the_right_frame_count(built):
    _, out, result, frames = built
    assert result["frames"] == 150 + 120 + 60 == len(frames)          # 60 frames at 29.97 last 120 show frames
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,r_frame_rate",
                                      "-of", "json", str(out)], capture_output=True, text=True).stdout)["streams"]
    assert {s["codec_type"] for s in info} == {"video", "audio"}
    assert next(s for s in info if s["codec_type"] == "video")["r_frame_rate"] == RATE


def test_source_segments_are_cut_by_frame_index(built):
    _, _, _, frames = built
    assert number(frames[0]) == 100 and number(frames[149]) == 249     # "out" is exclusive
    assert number(frames[270]) == 400 and number(frames[329]) == 459


def test_file_segment_is_conformed_in_place(built):
    _, _, _, frames = built
    for t in (150, 269):
        r, g, b = frames[t][540, 960]
        assert b > 200 and r < 40 and g < 40


def test_overlay_lands_on_its_frames(built):
    _, _, _, frames = built
    def white(t):
        return bool((frames[t][150, 250] > 230).all())
    # at=120 is 20 frames into take "a"; 30 frames at 29.97 cover 60 show frames
    assert not white(19) and white(20) and white(79) and not white(80)


def test_overlay_on_a_takes_first_frame_covers_that_frame(built):
    _, _, _, frames = built
    def white(t):
        return bool((frames[t][150, 250] > 230).all())
    assert not white(269) and white(270) and white(329)


def test_layout_json_says_where_everything_landed(built):
    d, _, _, _ = built
    layout = json.loads((d / "layout.json").read_text())
    assert layout["timeline"] == "Test Show v1" and layout["frames"] == 330
    assert [(s["id"], s["rec_in"], s["rec_out"]) for s in layout["segments"]] == [
        ("a", 0, 150), ("pkg", 150, 270), ("b", 270, 330)]
    assert layout["overlays"] == [{"id": "box", "rec_in": 20, "rec_out": 80, "covers": [122, 170]},
                                  {"id": "edge", "rec_in": 270, "rec_out": 330, "covers": None}]
    assert layout["cards"] == []


def test_fcpxml_parses_and_holds_the_edit(built):
    d, _, _, _ = built
    root = ET.parse(d / "timeline.fcpxml").getroot()
    assert root.tag == "fcpxml" and float(root.get("version")) >= 1.9
    spine = root.find("./library/event/project/sequence/spine")
    clips = list(spine)
    assert [c.get("name") for c in clips] == ["a", "pkg", "b"]
    assert clips[0].get("start") == "1001/600s"                         # source frame 100
    lanes = {c.get("lane"): c for c in clips[0].iter("asset-clip") if c is not clips[0]}
    assert lanes["3"].get("name") == "box" and lanes["-1"].get("name") == "a (mastered)"
    ids = {a.get("id") for a in root.iter("asset")}
    assert all(c.get("ref") in ids for c in root.iter("asset-clip"))


def test_audio_is_the_mastered_wavs_cut_to_picture(built):
    _, out, _, _ = built
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "f32le", "-ac", "1", "-ar", "48000", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.float32)
    assert abs(len(x) / 48000 - 330 / (60000 / 1001)) < 0.05
    assert np.abs(x[48000:49000]).max() > 0.3                          # the tone is there


def test_missing_mastered_audio_is_a_clear_error(tmp_path, built):
    d, _, _, _ = built
    edl = json.loads((d / "edl.json").read_text())
    (tmp_path / "edl.json").write_text(json.dumps(edl))
    with pytest.raises(RuntimeError, match="master_audio.py"):
        build_ffmpeg.build(str(tmp_path), str(tmp_path / "x.mp4"))


# ---------- fidelity against the editing-app render of a real show (private fixtures) ----------

import os  # noqa: E402

FIXTURES = Path(os.environ.get("INFOCUS_FIXTURES", Path.home() / "InFocusEditor/fixtures")) / "2026-10-21"
V4 = FIXTURES / "cases/v4"
needs_fixtures = pytest.mark.skipif(not (V4 / "render.mp4").exists(), reason="private 10-21 fixtures not built")


def ssim_per_frame(a, b, stats):
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(a), "-i", str(b), "-lavfi", f"ssim=stats_file={stats}",
                    "-f", "null", "-"], check=True)
    import re
    return [float(re.search(r"All:([\d.]+)", line).group(1)) for line in open(stats)]


@pytest.fixture(scope="module")
def fidelity(tmp_path_factory):
    import check_render
    import verify_render
    d = tmp_path_factory.mktemp("v4-ffmpeg")
    for name in ("analysis", "Foreground", "Graphics", "Audio", "edl.json"):
        (d / name).symlink_to(V4 / name)
    out = d / "render.mp4"
    result = build_ffmpeg.build(str(d), str(out), version=5)
    ref_layout = json.loads((V4 / "layout.json").read_text())
    layout = json.loads((d / "layout.json").read_text())
    edl = json.loads((d / "edl.json").read_text())
    ssim = ssim_per_frame(out, V4 / "render.mp4", d / "ssim.txt")
    windows = {f"{k[:-1]} {x['id']}": round(sum(ssim[x["rec_in"]:x["rec_out"]]) / (x["rec_out"] - x["rec_in"]), 4)
               for k in ("cards", "overlays") for x in layout[k]}
    peaks = {r["segment"]: r["peak"] for r in check_render.loudness_rows(str(out), layout, edl)}
    ref_peaks = {r["segment"]: r["peak"] for r in check_render.loudness_rows(str(V4 / "render.mp4"), ref_layout, edl)}
    verdict = verify_render.verify(str(d), str(out))
    report = {"encoder": result["encoder"], "frames": build_ffmpeg.video_frames(str(out)),
              "reference_frames": build_ffmpeg.video_frames(str(V4 / "render.mp4")),
              "layout_matches_reference": all(layout[k] == ref_layout[k] for k in ("segments", "cards", "frames")) and
              [(o["id"], o["rec_in"], o["rec_out"]) for o in layout["overlays"]] ==
              [(o["id"], o["rec_in"], o["rec_out"]) for o in ref_layout["overlays"]],
              "ssim_every_10th": round(sum(ssim[::10]) / len(ssim[::10]), 4), "ssim_frames_compared": len(ssim),
              "ssim_min": round(min(ssim), 4), "ssim_windows": windows,
              "peak_diff_db": {k: round(peaks[k] - ref_peaks[k], 2) for k in peaks if peaks[k] is not None},
              "verify_pass": verdict["pass"], "verify_failures": verdict["failures"]}
    (FIXTURES / "fidelity.json").write_text(json.dumps(report, indent=1))
    return report


@pytest.mark.fixtures
@needs_fixtures
def test_ffmpeg_build_matches_the_editing_app_render(fidelity):
    assert abs(fidelity["frames"] - fidelity["reference_frames"]) <= 2
    assert fidelity["layout_matches_reference"]
    assert fidelity["ssim_every_10th"] >= 0.97
    assert all(v >= 0.95 for v in fidelity["ssim_windows"].values()), fidelity["ssim_windows"]
    assert all(abs(v) <= 0.3 for v in fidelity["peak_diff_db"].values()), fidelity["peak_diff_db"]


@pytest.mark.fixtures
@needs_fixtures
def test_verify_render_passes_on_the_ffmpeg_build(fidelity):
    assert fidelity["verify_pass"], fidelity["verify_failures"]
