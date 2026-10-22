"""Several recordings and separate camera feeds: the build, the camera classifier and the multicam verify rules,
on small synthetic media."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import analyze_cameras  # noqa: E402
import build_ffmpeg  # noqa: E402
import common  # noqa: E402
import fixups  # noqa: E402
import verify_render  # noqa: E402

RATE = "60000/1001"
FPS = 60000 / 1001
STEP = 32
MARK = {"M": (255, 0, 0), "L": (0, 255, 0), "R": (0, 0, 255), "program": (255, 255, 0)}


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def coded_feed(path, first_world, frames, mark):
    """A feed whose frame k shows world frame first_world + k (base-8 digits x32 in r, g, b at the centre) and a
    corner patch in the camera's colour."""
    r, g, b = mark
    ffmpeg("-f", "lavfi", "-i", f"color=black:s=1920x1080:r={RATE}:d={frames / FPS + 0.1:.4f}",
           "-vf", f"geq=r='mod(N+{first_world}\\,8)*32':g='mod(floor((N+{first_world})/8)\\,8)*32':"
                  f"b='floor((N+{first_world})/64)*32',drawbox=x=0:y=0:w=200:h=200:color=0x{r:02x}{g:02x}{b:02x}:t=fill",
           "-frames:v", str(frames), "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv444p", "-g", "60", str(path))


def world(rgb):
    r, g, b = (int(round(v / STEP)) for v in rgb[540, 960, :3])
    return r + 8 * g + 64 * b


def mark_of(rgb):
    px = rgb[100, 100, :3].astype(int)
    return min(MARK, key=lambda k: np.abs(px - np.array(MARK[k])).sum())


@pytest.fixture(scope="module")
def two_recordings(tmp_path_factory):
    d = tmp_path_factory.mktemp("show")
    # r1: three camera files recorded together, each starting at a different world frame
    coded_feed(d / "m.mp4", 20, 580, MARK["M"])          # file frame k = world 20 + k
    coded_feed(d / "l.mp4", 0, 580, MARK["L"])           # world k
    coded_feed(d / "r.mp4", 35, 565, MARK["R"])          # world 35 + k   -> the common start is world 35
    coded_feed(d / "p2.mp4", 300, 200, MARK["program"])  # r2: one switched program file
    box = np.zeros((1080, 1920, 4), np.uint8)
    box[300:400, 300:600] = 255
    Image.fromarray(box, "RGBA").save(d / "box.png")
    ffmpeg("-loop", "1", "-framerate", "30000/1001", "-i", str(d / "box.png"), "-frames:v", "10", *fixups.PRORES,
           str(d / "box.mov"))
    recs = [{"id": "r1", "kind": "multicam", "frames": 545,
             "files": [{"path": str(d / "m.mp4"), "role": "M", "offsetFrames": 15},
                       {"path": str(d / "l.mp4"), "role": "L", "offsetFrames": 35},
                       {"path": str(d / "r.mp4"), "role": "R", "offsetFrames": 0}]},
            {"id": "r2", "kind": "program", "frames": 200,
             "files": [{"path": str(d / "p2.mp4"), "role": "program", "offsetFrames": 0}]}]
    edl = {"show": "Test Show", "fps": RATE, "recordings": recs,
           "segments": [{"id": "open", "kind": "source", "recording": "r1", "cam": "M", "in": 10, "out": 100},
                        {"id": "ann", "kind": "source", "recording": "r1", "cam": "L", "in": 100, "out": 200},
                        {"id": "take2", "kind": "source", "recording": "r2", "in": 50, "out": 110},
                        {"id": "back", "kind": "source", "recording": "r1", "cam": "R", "in": 200, "out": 260}],
           "overlays": [{"id": "bar1", "recording": "r1", "at": 120, "file": str(d / "box.mov")},
                        {"id": "bar2", "recording": "r2", "at": 60, "file": str(d / "box.mov")}]}
    (d / "edl.json").write_text(json.dumps(edl))
    (d / "Audio").mkdir()
    for seg in edl["segments"]:
        sf.write(d / "Audio" / f"{seg['id']}.wav", np.zeros((int(2.2 * 48000), 2), np.float32), 48000)
    out = d / "render.mp4"
    build_ffmpeg.build(str(d), str(out))
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return d, np.frombuffer(raw, np.uint8).reshape(-1, 1080, 1920, 3)


@pytest.mark.parametrize("t,cam,world_frame", [
    (0, "M", 35 + 10), (89, "M", 35 + 99),               # r1 frame f is world 35 + f on every camera
    (90, "L", 35 + 100), (189, "L", 35 + 199),
    (190, "program", 300 + 50), (249, "program", 300 + 109),
    (250, "R", 35 + 200), (309, "R", 35 + 259)])
def test_each_take_reads_its_camera_file_at_in_plus_offset(two_recordings, t, cam, world_frame):
    _, frames = two_recordings
    assert mark_of(frames[t]) == cam
    assert world(frames[t]) == world_frame


def test_overlays_land_on_their_own_recordings_frames(two_recordings):
    d, frames = two_recordings
    def white(t):
        return bool((frames[t][350, 450] > 230).all())
    # bar1: r1 frame 120 = 20 into "ann" -> render 110; bar2: r2 frame 60 = 10 into "take2" -> render 200
    assert not white(109) and white(110) and white(129) and not white(130)
    assert not white(199) and white(200) and white(219) and not white(220)
    layout = json.loads((d / "layout.json").read_text())
    assert [(o["id"], o["rec_in"]) for o in layout["overlays"]] == [("bar1", 110), ("bar2", 200)]


def test_an_overlay_without_its_recording_is_refused_when_there_are_several(two_recordings, tmp_path):
    d, _ = two_recordings
    edl = json.loads((d / "edl.json").read_text())
    del edl["overlays"][0]["recording"]
    with pytest.raises(RuntimeError, match="recording"):
        build_ffmpeg.plan(edl, str(d))


def test_segment_sources_follow_the_recording():
    edl = {"recordings": [{"id": "r1", "kind": "multicam", "files": [
        {"path": "/x/m.mov", "role": "M", "offsetFrames": 4}, {"path": "/x/l.mov", "role": "L", "offsetFrames": 9}]},
        {"id": "r2", "kind": "program", "files": [{"path": "/x/p.mov", "role": "program", "offsetFrames": 0}]}]}
    assert common.segment_source(edl, {"id": "a", "recording": "r1", "cam": "L"}) == ("/x/l.mov", 9)
    assert common.segment_audio(edl, {"id": "a", "recording": "r1", "cam": "L"}) == ("/x/m.mov", 4)
    assert common.segment_source(edl, {"id": "b", "recording": "r2", "cam": "R"}) == ("/x/p.mov", 0)
    with pytest.raises(RuntimeError, match="no 'R' camera"):
        common.segment_source(edl, {"id": "c", "recording": "r1", "cam": "R"})
    legacy = {"source": "/x/show.mov"}
    assert common.segment_source(legacy, {"id": "d", "cam": "L"}) == ("/x/show.mov", 0)


# ---------- the camera classifier (the worker's picture profile) ----------

def test_classify_frames_profiles_a_file(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "04 Virtual Sets").mkdir(parents=True)
    rng = np.random.default_rng(1)
    middle = rng.integers(60, 200, (1080, 1920, 3), dtype=np.uint8)
    Image.fromarray(middle).save(pkg / "04 Virtual Sets" / "InFocus Virtual Set 2026 - Middle.png")
    Image.fromarray(middle).save(tmp_path / "m.png")
    ffmpeg("-loop", "1", "-framerate", "1", "-i", str(tmp_path / "m.png"), "-t", "4", "-c:v", "libx264", "-qp", "0",
           "-pix_fmt", "yuv444p", str(tmp_path / "m.mp4"))
    ffmpeg("-f", "lavfi", "-i", "color=black:s=1920x1080:r=1:d=4", "-c:v", "libx264", str(tmp_path / "b.mp4"))
    assert set(analyze.classify_frames(str(tmp_path / "m.mp4"), 2.0, str(pkg))) == {"M"}
    assert analyze.classify_frames(str(tmp_path / "b.mp4"), 2.0, str(pkg)) == ["BLACK", "BLACK"]


# ---------- multicam verify rules ----------

MC = {"recordings": [{"id": "r1", "kind": "multicam", "frames": 3000,
                      "files": [{"path": "/x/m.mov", "role": "M", "offsetFrames": 0}]}], "fps": RATE}


def take(i, cam, a, b, **kw):
    return {"id": i, "kind": "source", "recording": "r1", "cam": cam, "in": a, "out": b, **kw}


def test_no_shot_under_1_5_s():
    edl = {**MC, "segments": [take("open", "M", 0, 300), take("ann", "L", 300, 380, card={}),
                              take("close", "M", 380, 700)]}
    fails = verify_render.rule_shot_length(edl)
    assert [(f["ruleNumber"], f["segment"]) for f in fails] == [(9, "ann")]       # 80 frames = 1.33 s


def test_contiguous_takes_on_one_camera_are_one_shot():
    edl = {**MC, "segments": [take("a", "M", 0, 50), take("b", "M", 50, 120), take("c", "L", 120, 400, card={})]}
    assert verify_render.rule_shot_length(edl) == []                               # 120 frames together = 2 s


def test_short_program_takes_are_not_held_to_the_shot_rule():
    edl = {"source": "/x/p.mov", "fps": RATE, "segments": [{"id": "a", "kind": "source", "in": 0, "out": 30}]}
    assert verify_render.rule_shot_length(edl) == []


def test_side_set_rule_needs_the_set_and_a_card():
    edl = {**MC, "segments": [take("ann", "L", 0, 100, card={"title": "x"}), take("ann2", "R", 100, 200)]}
    layout = {"segments": [{"id": "ann", "kind": "source", "rec_in": 0, "rec_out": 100},
                           {"id": "ann2", "kind": "source", "rec_in": 100, "rec_out": 200}]}
    cams = {"ann": ["L"] * 50 + ["M"] * 50, "ann2": ["R"] * 100}
    fails = verify_render.rule_side_set(cams, edl, layout)
    assert {(f["segment"], "card" in f["detail"]) for f in fails} == {("ann", False), ("ann2", True)}
    carded = {**edl, "segments": [take("ann", "L", 0, 100, card={"title": "x"}),
                                  take("ann2", "R", 100, 200, card={"title": "y"})]}
    assert verify_render.rule_side_set({"ann": ["L"] * 100, "ann2": ["R"] * 100}, carded, layout) == []


def test_a_take_past_its_recording_fails_rule_11():
    edl = {**MC, "segments": [take("a", "M", 2900, 3100)]}
    assert [f["ruleNumber"] for f in verify_render.rule_recording_range(edl)] == [11]


@pytest.mark.parametrize("shift_frames,ok", [(0, True), (1, True), (3, False)])
def test_av_sync_is_checked_against_each_cameras_own_sound(tmp_path, shift_frames, ok):
    """The render's sound in an L take is compared with the L file's own sound at the same frames."""
    sr = 48000
    rng = np.random.default_rng(3)
    noise = rng.standard_normal(12 * sr).astype(np.float32) * 0.2
    sf.write(tmp_path / "m.wav", np.stack([noise, noise], 1), sr)
    sf.write(tmp_path / "l.wav", np.stack([noise, noise], 1), sr)
    shift = int(round(shift_frames / FPS * sr))
    render = noise[int(200 / FPS * sr) - shift:]          # the L take starts at file frame 200, shifted
    sf.write(tmp_path / "render.wav", np.stack([render, render], 1), sr)
    edl = {"fps": RATE, "recordings": [{"id": "r1", "kind": "multicam", "frames": 700, "files": [
        {"path": str(tmp_path / "m.wav"), "role": "M", "offsetFrames": 0},
        {"path": str(tmp_path / "l.wav"), "role": "L", "offsetFrames": 0}]}],
        "segments": [take("ann", "L", 200, 500)]}
    layout = {"segments": [{"id": "ann", "kind": "source", "rec_in": 0, "rec_out": 300}]}
    fails, notes = verify_render.rule_av_sync(str(tmp_path / "render.wav"), edl, layout)
    assert (fails == []) is ok, fails
    assert not notes


def test_program_recordings_keep_their_own_analysis(tmp_path):
    edl = {"fps": RATE, "recordings": [
        {"id": "r1", "kind": "program", "files": [{"path": "/x/a.mov", "role": "program", "offsetFrames": 0}]},
        {"id": "r2", "kind": "program", "files": [{"path": "/x/b.mov", "role": "program", "offsetFrames": 0}]}],
        "segments": [{"id": "a", "kind": "source", "recording": "r1", "in": 0, "out": 10},
                     {"id": "b", "kind": "source", "recording": "r2", "in": 0, "out": 10}],
        "overlays": [{"id": "o", "recording": "r2", "at": 3, "covers": [3, 9]}]}
    view = verify_render.recording_view(edl, "r2")
    assert view["source"] == "/x/b.mov" and [s["id"] for s in view["segments"]] == ["b"]
    assert [o["id"] for o in view["overlays"]] == ["o"]
    (tmp_path / "analysis" / "r2").mkdir(parents=True)
    (tmp_path / "analysis" / "r2" / "analysis.json").write_text('{"shots": [], "baked_overlays": []}')
    assert verify_render.load_analysis(str(tmp_path), edl, "r2") == {"shots": [], "baked_overlays": []}
    with pytest.raises(FileNotFoundError):
        verify_render.load_analysis(str(tmp_path), edl, "r1")


# ---------- multicam analysis helpers ----------

def test_mouth_tool_reads_lip_gaps(tmp_path):
    """Vision finds no face in a blank clip: the tool runs and reports zero faces per sample."""
    ffmpeg("-f", "lavfi", "-i", f"color=gray:s=640x360:r={RATE}:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           str(tmp_path / "blank.mp4"))
    rows = analyze_cameras.mouth_track(str(tmp_path / "blank.mp4"), 0, 1, 6)
    assert len(rows) >= 8 and all(faces == [] for _, faces in rows)


def test_reader_prefers_mouth_movement_then_the_louder_mic():
    assert analyze_cameras.reader({"L": 2.0, "R": 0.5}, {}) == ("L", "mouth movement")
    assert analyze_cameras.reader({"L": 1.0, "R": 0.95}, {"L": -30.0, "R": -22.0}) == ("R", "loudest mic")
    assert analyze_cameras.reader({"L": 1.0, "R": 0.95}, {"L": -25.0, "R": -24.0})[0] is None
    assert analyze_cameras.reader({"L": 0.025, "R": 0.005}, {})[0] is None           # nobody's mouth is moving


def test_feed_runs_flag_a_frozen_feed():
    st = {"L": {"cam": ["L"] * 300, "diff": [2.0] * 100 + [0.0] * 200}}
    info = analyze_cameras.feed_runs(st, FPS)["L"]
    assert info["shows_own_set"] == 1.0 and info["frozen"] == [[100, 299]]


# ---------- the pseudo-ISO 10-21 show: two recordings x three cameras (private fixtures) ----------

import os  # noqa: E402

ISO = Path(os.environ.get("INFOCUS_FIXTURES", Path.home() / "InFocusEditor/fixtures")) / "2026-10-21-iso"
needs_iso = pytest.mark.skipif(not (ISO / "show" / "Audio" / "levels.json").exists(),
                               reason="private pseudo-ISO fixtures not built (build_iso.py)")
SLACK = 60                                                   # ±1 s at 59.94


def plan_cuts(edl):
    """Camera changes in a switching plan: consecutive takes of one recording on different cameras."""
    takes = [s for s in edl["segments"] if s["kind"] == "source"]
    return [{"recording": a["recording"], "from": a["cam"], "out": a["out"], "to": b["cam"], "in": b["in"]}
            for a, b in zip(takes, takes[1:]) if a["recording"] == b["recording"] and a["cam"] != b["cam"]]


def match_cuts(plan, switcher, edl):
    """Each plan cut matched to a switcher cut (same cameras, both edges within SLACK), and every switcher cut inside
    the span the plan uses matched back. Returns (deltas, unmatched plan cuts, unmatched switcher cuts)."""
    deltas, loose_plan = [], []
    for c in plan:
        hits = [s for s in switcher[c["recording"]] if s["from"] == c["from"] and s["to"] == c["to"]
                and abs(s["out"] - c["out"]) <= SLACK and abs(s["in"] - c["in"]) <= SLACK]
        if hits:
            s = min(hits, key=lambda s: abs(s["out"] - c["out"]) + abs(s["in"] - c["in"]))
            deltas.append({"recording": c["recording"], "cut": f"{c['from']}→{c['to']}",
                           "out_delta": c["out"] - s["out"], "in_delta": c["in"] - s["in"]})
        else:
            loose_plan.append(c)
    loose_switcher = []
    for rid, cuts in switcher.items():
        takes = [s for s in edl["segments"] if s["kind"] == "source" and s.get("recording") == rid]
        if not takes:
            continue
        lo, hi = min(t["in"] for t in takes), max(t["out"] for t in takes)
        for s in cuts:
            if lo < s["in"] and s["out"] < hi and not any(
                    c["recording"] == rid and c["from"] == s["from"] and c["to"] == s["to"]
                    and abs(s["out"] - c["out"]) <= SLACK and abs(s["in"] - c["in"]) <= SLACK for c in plan):
                loose_switcher.append({**s, "recording": rid})
    return deltas, loose_plan, loose_switcher


@pytest.fixture(scope="module")
def iso_build(tmp_path_factory):
    d = tmp_path_factory.mktemp("iso-show")
    for name in ("Graphics", "Foreground", "Audio", "edl.json"):
        (d / name).symlink_to(ISO / "show" / name)
    result = build_ffmpeg.build(str(d), str(d / "render.mp4"))
    verdict = verify_render.verify(str(d), str(d / "render.mp4"))
    edl = json.loads((d / "edl.json").read_text())
    truth = json.loads((ISO / "truth.json").read_text())
    deltas, loose_plan, loose_switcher = match_cuts(plan_cuts(edl), truth["switcher_cuts"], edl)
    report = {"frames": result["frames"], "encoder": result["encoder"], "verify_pass": verdict["pass"],
              "verify_failures": verdict["failures"], "verify_notes": verdict["notes"],
              "cut_deltas": deltas, "unmatched_plan_cuts": loose_plan, "unmatched_switcher_cuts": loose_switcher,
              "loudness": verdict["loudness"]}
    (ISO / "report.json").write_text(json.dumps(report, indent=1))
    return report


@pytest.mark.fixtures
@needs_iso
def test_the_hand_written_switching_plan_builds_and_passes_verify(iso_build):
    assert iso_build["verify_pass"], iso_build["verify_failures"]
    assert not iso_build["verify_notes"], iso_build["verify_notes"]     # every camera's sync was really checked


@pytest.mark.fixtures
@needs_iso
def test_the_plan_lands_within_a_second_of_the_switchers_cuts(iso_build):
    assert not iso_build["unmatched_plan_cuts"] and not iso_build["unmatched_switcher_cuts"]
    assert len(iso_build["cut_deltas"]) >= 5
    assert all(abs(d["out_delta"]) <= SLACK and abs(d["in_delta"]) <= SLACK for d in iso_build["cut_deltas"])


def test_cut_matching_rejects_a_wrong_camera_and_a_late_cut():
    switcher = {"r1": [{"from": "M", "out": 1000, "to": "R", "in": 1000}]}
    edl = {"segments": [take("a", "M", 0, 1000), take("b", "R", 1000, 2000)]}
    assert match_cuts(plan_cuts(edl), switcher, edl)[1:] == ([], [])
    late = {"segments": [take("a", "M", 0, 1090), take("b", "R", 1090, 2000)]}
    _, loose_plan, loose_switcher = match_cuts(plan_cuts(late), switcher, late)
    assert loose_plan and loose_switcher
    wrong = {"segments": [take("a", "M", 0, 1000), take("b", "L", 1000, 2000, card={"title": "x"})]}
    assert match_cuts(plan_cuts(wrong), switcher, wrong)[1]
