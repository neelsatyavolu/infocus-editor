"""verify_render.py on re-composited shows: rule 12 (one set for the whole show, no fix-up clip from the old
recording over a re-composited take), rule 4 leaving re-composited takes alone, and a re-composited side take on a
seasonal set still reading as its side. Synthetic data only."""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
import common  # noqa: E402
import verify_render  # noqa: E402

TAKES = [{"id": "open", "kind": "source", "cam": "M", "in": 100, "out": 900},
         {"id": "chess", "kind": "source", "cam": "R", "in": 900, "out": 2000, "card": {"title": "Chess"}}]


def show_dir(tmp_path, plan, theme="", overlays=()):
    edl = {"show": "Test Show", "fps": "60000/1001", "recomposite": True, "theme": theme,
           "source": "/rec.mov", "segments": TAKES, "overlays": list(overlays)}
    if plan is not None:
        (tmp_path / "Recomposite").mkdir()
        (tmp_path / "Recomposite" / "plan.json").write_text(json.dumps(plan))
    return str(tmp_path), edl


def plan(theme, *takes):
    edl = {"source": "/rec.mov", "segments": TAKES, "overlays": [], "theme": theme}
    sig = {s["id"]: common.take_signature("", edl, s) for s in TAKES}
    return {"theme": theme, "abandoned": None,
            "takes": [{"id": i, "status": st, "recorded_theme": rec, "source": sig[i]} for i, st, rec in takes]}


def test_mixed_sets_fail_rule_12(tmp_path):
    d, edl = show_dir(tmp_path, plan("halloween", ("open", "recomposited", ""), ("chess", "fallback", "")),
                      theme="halloween")
    fails = verify_render.rule_set(d, edl)
    assert [f["ruleNumber"] for f in fails] == [12]
    assert "chess" in fails[0]["detail"] and "halloween" in fails[0]["detail"]


def test_same_set_fallback_passes(tmp_path):
    d, edl = show_dir(tmp_path, plan("", ("open", "recomposited", ""), ("chess", "fallback", "")))
    assert verify_render.rule_set(d, edl) == []


def test_a_plan_for_another_theme_fails_rule_12(tmp_path):
    d, edl = show_dir(tmp_path, plan("", ("open", "recomposited", ""), ("chess", "recomposited", "")),
                      theme="halloween")
    fails = verify_render.rule_set(d, edl)
    assert {f["ruleNumber"] for f in fails} == {12} and any("run recomposite.py again" in f["detail"] for f in fails)


def test_recomposite_on_without_a_plan_fails_rule_12(tmp_path):
    d, edl = show_dir(tmp_path, None)
    fails = verify_render.rule_set(d, edl)
    assert [f["ruleNumber"] for f in fails] == [12] and "recomposite.py" in fails[0]["detail"]


def test_a_fixup_clip_over_a_recomposited_take_fails_rule_12(tmp_path):
    fix = {"id": "hold", "at": 950, "covers": [950, 980], "file": str(tmp_path / "Graphics/fixes/hold_f981_31f.mov")}
    d, edl = show_dir(tmp_path, None, overlays=[fix])
    (tmp_path / "Recomposite").mkdir()
    (tmp_path / "Recomposite" / "plan.json").write_text(json.dumps(signed_plan(edl, d)))
    fails = verify_render.rule_set(d, edl)
    assert [f["ruleNumber"] for f in fails] == [12] and "hold" in fails[0]["detail"]


def test_a_show_without_recomposite_has_no_rule_12(tmp_path):
    d, edl = show_dir(tmp_path, None)
    assert verify_render.rule_set(d, {**edl, "recomposite": False}) == []


def test_rule_4_leaves_recomposited_takes_alone():
    edl = {"segments": TAKES, "overlays": []}
    analysis = {"shots": [], "baked_overlays": [{"zone": "Follow card", "first": 300, "last": 400}]}
    assert [f["ruleNumber"] for f in verify_render.rule_uncovered(edl, analysis)] == [4]
    assert verify_render.rule_uncovered(edl, analysis, recomposited={"open", "chess"}) == []


def test_recomposited_side_take_still_reads_as_its_set(tmp_path):
    rng = np.random.default_rng(7)
    left = rng.integers(60, 200, (1080, 1920, 3), dtype=np.uint8)          # a seasonal Left set
    x, y, w, h = common.SCREENS["L"]
    frame = left.copy()
    frame[y:y + h, x:x + w] = (15, 17, 15)                                   # a fresh card on the TV
    frame[300:1080, 1350:1750] = (40, 60, 160)                               # the anchor
    mid = rng.integers(60, 200, (1080, 1920, 3), dtype=np.uint8)
    png = tmp_path / common.THEMES[""]["middle"]
    png.parent.mkdir(parents=True)
    Image.fromarray(mid).save(png)
    small = np.asarray(Image.fromarray(frame).resize((analyze.SW, analyze.SH), Image.BOX))
    assert analyze.classify(small, analyze.middle_set(str(tmp_path))) == "L"


def signed_plan(edl, d, theme=""):
    return {"theme": theme, "abandoned": None,
            "takes": [{"id": s["id"], "status": "recomposited", "recorded_theme": theme,
                       "source": common.take_signature(d, edl, s)} for s in edl["segments"] if s["kind"] == "source"]}


def test_a_take_changed_since_recompositing_fails_rule_12(tmp_path):
    d, edl = show_dir(tmp_path, None)
    (tmp_path / "Recomposite").mkdir()
    (tmp_path / "Recomposite" / "plan.json").write_text(json.dumps(signed_plan(edl, d)))
    assert verify_render.rule_set(d, edl) == []
    moved = {**edl, "segments": [{**TAKES[0], "in": 145}, TAKES[1]]}        # a fix cut a stumble from the open
    fails = verify_render.rule_set(d, moved)
    assert [f["ruleNumber"] for f in fails] == [12] and "open" in fails[0]["detail"] and "--only" in fails[0]["detail"]
    retyped = {**edl, "segments": [TAKES[0], {**TAKES[1], "card": {"title": "Chess Club"}}]}
    assert [f["segment"] for f in verify_render.rule_set(d, retyped)] == ["chess"]


def test_a_take_missing_from_the_plan_fails_rule_12(tmp_path):
    d, edl = show_dir(tmp_path, None)
    (tmp_path / "Recomposite").mkdir()
    plan = signed_plan(edl, d)
    plan["takes"] = plan["takes"][:1]
    (tmp_path / "Recomposite" / "plan.json").write_text(json.dumps(plan))
    fails = verify_render.rule_set(d, edl)
    assert [f["segment"] for f in fails] == ["chess"]


def test_a_theme_change_makes_every_take_stale(tmp_path):
    d, edl = show_dir(tmp_path, None)
    (tmp_path / "Recomposite").mkdir()
    (tmp_path / "Recomposite" / "plan.json").write_text(json.dumps(signed_plan(edl, d)))
    fails = verify_render.rule_set(d, {**edl, "theme": "halloween"})
    assert {f["segment"] for f in fails if f.get("segment")} == {"open", "chess"}


def test_a_fixup_running_into_a_recomposited_take_fails_rule_12(tmp_path):
    fix = {"id": "hold", "at": 880, "covers": [880, 930], "file": str(tmp_path / "Graphics/fixes/hold_f931_51f.mov")}
    d, edl = show_dir(tmp_path, None, overlays=[fix])
    (tmp_path / "Recomposite").mkdir()
    plan = signed_plan(edl, d)
    plan["takes"][0]["status"] = "fallback"                    # the fix-up starts in a take that kept its picture
    (tmp_path / "Recomposite" / "plan.json").write_text(json.dumps(plan))
    assert any("hold" in f["detail"] and f.get("segment") == "chess" for f in verify_render.rule_set(d, edl))


def test_rule_4_trusts_what_the_render_used_not_a_newer_plan():
    layout = {"segments": [{"id": "open", "recomposite": True}, {"id": "chess"}]}
    assert verify_render.rendered_recomposited(layout, {"open", "chess"}) == {"open"}


def test_changing_a_takes_seat_makes_it_stale(tmp_path):
    d, edl = show_dir(tmp_path, None)
    (tmp_path / "Recomposite").mkdir()
    (tmp_path / "Recomposite" / "plan.json").write_text(json.dumps(signed_plan(edl, d)))
    seated = {**edl, "segments": [{**TAKES[0], "seat": {"scale": 1.0, "x": 0, "y": 12}}, TAKES[1]]}
    assert [f["segment"] for f in verify_render.rule_set(d, seated)] == ["open"]
