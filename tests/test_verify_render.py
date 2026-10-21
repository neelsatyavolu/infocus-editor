"""verify_render.py: code, not the agent, decides whether an edit is finished.

The fixture tests replay the three mistakes made on a real show (10-21-2026) before its approved v4: they need the
private fixtures in ~/InFocusEditor/fixtures/2026-10-21 (built by build_cases.py there) and are skipped without them.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_render  # noqa: E402

FIXTURES = Path(os.environ.get("INFOCUS_FIXTURES", Path.home() / "InFocusEditor/fixtures")) / "2026-10-21"
CASES = FIXTURES / "cases"
ORIGINAL_PREVIEW = FIXTURES / "InFocus Show 10-21-2026 Edit/InFocus Show 10-21-2026 (preview).mp4"
needs_fixtures = pytest.mark.skipif(not (CASES / "v4/render.mp4").exists(), reason="private 10-21 fixtures not built")

INTRO = "{PACKAGE}/03 Intro & Outro/InFocus Intro 2026.mp4"
OUTRO = "{PACKAGE}/03 Intro & Outro/InFocus Outro 2026.mp4"


# ---------- rules on data alone (no render, no fixtures) ----------

def show(segments, overlays=(), shots=None, baked=()):
    edl = {"show": "Test Show", "fps": "60000/1001", "segments": list(segments), "overlays": list(overlays)}
    analysis = {"frames": 9000,
                "shots": shots or [{"start": 0, "end": 1000, "cam": "OTHER"}, {"start": 1000, "end": 8000, "cam": "M"},
                                   {"start": 8000, "end": 8300, "cam": "FS"}, {"start": 8300, "end": 9000, "cam": "M"}],
                "baked_overlays": list(baked)}
    return edl, analysis


GOOD = [{"id": "intro", "kind": "file", "path": INTRO},
        {"id": "open", "kind": "source", "in": 1100, "out": 2000},
        {"id": "outro", "kind": "file", "path": OUTRO}]


def rules(failures):
    return sorted({f["ruleNumber"] for f in failures})


def test_a_well_formed_show_passes_rules_4_and_5():
    edl, analysis = show(GOOD)
    assert verify_render.rule_uncovered(edl, analysis) == []
    assert verify_render.rule_intro_outro(edl, analysis) == []


def test_a_show_without_the_package_intro_fails_rule_5():
    edl, analysis = show(GOOD[1:])
    fails = verify_render.rule_intro_outro(edl, analysis)
    assert rules(fails) == [5] and "intro" in fails[0]["detail"].lower()


def test_a_take_inside_the_switchers_intro_or_end_card_fails_rule_5():
    edl, analysis = show([GOOD[0], {"id": "open", "kind": "source", "in": 600, "out": 2000},
                          {"id": "close", "kind": "source", "in": 7000, "out": 8100}, GOOD[2]])
    fails = verify_render.rule_intro_outro(edl, analysis)
    assert {f["segment"] for f in fails} == {"open", "close"}


def test_an_old_graphic_without_a_cover_fails_rule_4():
    baked = [{"zone": "name bar (left)", "first": 1200, "last": 1500}]
    edl, analysis = show(GOOD, baked=baked)
    fails = verify_render.rule_uncovered(edl, analysis)
    assert rules(fails) == [4] and fails[0]["frames"] == [1200, 1500]
    covered = [{"id": "lower", "at": 1198, "covers": [1200, 1500], "render": {"type": "lower"}}]
    edl, analysis = show(GOOD, covered, baked=baked)
    assert verify_render.rule_uncovered(edl, analysis) == []


def test_an_old_graphic_outside_every_take_needs_no_cover():
    edl, analysis = show(GOOD, baked=[{"zone": "Follow card", "first": 5000, "last": 5400}])
    assert verify_render.rule_uncovered(edl, analysis) == []


def test_a_side_camera_segment_without_a_card_fails_rule_4():
    shots = [{"start": 0, "end": 1000, "cam": "OTHER"}, {"start": 1000, "end": 3000, "cam": "L"},
             {"start": 3000, "end": 9000, "cam": "M"}]
    edl, analysis = show([GOOD[0], {"id": "ann", "kind": "source", "in": 1100, "out": 2900}, GOOD[2]], shots=shots)
    fails = verify_render.rule_uncovered(edl, analysis)
    assert rules(fails) == [4] and fails[0]["segment"] == "ann"


def test_rule_6_fails_a_quiet_segment_and_a_loud_anchor(tmp_path):
    sr, fps = 48000, 60000 / 1001
    t = np.arange(3 * sr) / sr
    tone = np.sin(2 * np.pi * 1000 * t)
    x = np.concatenate([tone * 10 ** (-6 / 20), tone * 10 ** (-7.7 / 20)])
    sf.write(tmp_path / "show.wav", np.stack([x, x], 1).astype(np.float32), sr)
    seg = round(3 * fps)
    layout = {"segments": [{"id": "open", "kind": "source", "rec_in": 0, "rec_out": seg},
                           {"id": "throw", "kind": "source", "rec_in": seg, "rec_out": 2 * seg}]}
    edl = {"fps": "60000/1001", "audio": {"peak_dbfs": -6, "dialogue_lufs": -19.5}}
    fails, rows = verify_render.rule_audio(str(tmp_path / "show.wav"), layout, edl, levels={})
    by_seg = {(f["segment"], f["detail"].split(":")[0]) for f in fails}
    assert ("throw", "peak") in by_seg                       # -7.7 dBFS
    assert ("open", "loudness") in by_seg                    # a -6 dBFS sine is far louder than -19.5 LUFS dialogue
    assert len(rows) == 2


def test_cli_exits_2_without_a_layout(tmp_path):
    (tmp_path / "edl.json").write_text(json.dumps({"show": "X", "fps": "60000/1001", "segments": []}))
    r = subprocess.run([sys.executable, str(SCRIPTS / "verify_render.py"), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 2


# ---------- the real 10-21 mistakes ----------

def verify(case, render=None):
    d = CASES / case
    cmd = [sys.executable, str(SCRIPTS / "verify_render.py"), str(d), "--render", str(render or d / "render.mp4")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode in (0, 1), r.stderr
    out = json.loads((d / "check/verify.json").read_text())
    assert (r.returncode == 0) == out["pass"]
    return out, json.loads((d / "edl.json").read_text())


@pytest.fixture(scope="module")
def results():
    return {}


def cached(results, key, *args):
    if key not in results:
        results[key] = verify(*args)
    return results[key]


@pytest.mark.fixtures
@needs_fixtures
def test_v4_passes_all_rules(results):
    out, _ = cached(results, "v4", "v4")
    assert out["failures"] == [] and out["pass"] is True
    assert all(row["ok"] for row in out["loudness"])


@pytest.mark.fixtures
@needs_fixtures
def test_v1_fails_rule_2_for_the_dual_third(results):
    out, edl = cached(results, "v1", "v1")
    dual = next(o["id"] for o in edl["overlays"] if o.get("render", {}).get("type") == "dual")
    span = [f for f in out["failures"] if f["ruleNumber"] == 2]
    assert span and all(f["overlay"] == dual for f in span)
    a, b = span[0]["frames"]
    cover = next(o for o in edl["overlays"] if o["id"] == dual)["covers"]
    assert cover[0] < a and b == cover[1]                     # the tail of the old dual third is uncovered
    assert not out["pass"]


@pytest.mark.fixtures
@needs_fixtures
def test_v2_fails_rule_1_at_the_hold(results):
    out, edl = cached(results, "v2", "v2")
    hits = [f for f in out["failures"] if f["ruleNumber"] == 1]
    assert len(hits) == 1 and hits[0]["segment"] == "throw"
    a, b = hits[0]["frames"]
    assert b - a + 1 >= 29                                    # every frame of the hold shows the full-screen card
    assert not out["pass"]


@pytest.mark.fixtures
@needs_fixtures
def test_v3_fails_rule_1_on_one_frame(results):
    out, _ = cached(results, "v3", "v3")
    hits = [f for f in out["failures"] if f["ruleNumber"] == 1]
    assert len(hits) == 1 and hits[0]["segment"] == "throw"
    a, b = hits[0]["frames"]
    assert a == b                                             # exactly one full-screen frame
    assert not out["pass"]


@pytest.mark.fixtures
@needs_fixtures
def test_the_minus_7_7_throw_fails_rule_6(results):
    out, _ = cached(results, "v4-original-audio", "v4", ORIGINAL_PREVIEW)
    assert rules(out["failures"]) == [6]
    throw = next(r for r in out["loudness"] if r["segment"] == "throw")
    assert throw["ok"] is False and throw["peak"] == pytest.approx(-7.7, abs=0.3)
