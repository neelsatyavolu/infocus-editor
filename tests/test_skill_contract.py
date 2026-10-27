"""The skill's side of docs/job-contract.md: status.json via progress.py, and the result.json schema."""
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude/skills/infocus-show"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import progress  # noqa: E402

SCHEMA = json.loads((ROOT / "tests/result.schema.json").read_text())

EXAMPLE_RESULT = {
    "output": "/jobs/2026-10-21-abc/out/InFocus Show 2026-10-21 v1.mp4",
    "poster": "/jobs/2026-10-21-abc/out/InFocus Show 2026-10-21 v1.poster.jpg",
    "decisions": [
        {"kind": "name", "detail": "Dual third: filmed 'Abby S.', used the Portal spelling 'Abby Smith'", "flagged": False},
        {"kind": "package", "detail": "Anchors throw to a second package that isn't queued: placeholder", "flagged": True},
    ],
    "checks": {"coversClean": True,
               "loudness": [{"segment": "open", "lufs": -19.5, "peak": -6.0, "ok": True}],
               "failures": []},
    "needsReview": True,
    "reply": None,
    "abort": None,
}


def test_progress_writes_status_json_atomically(tmp_path):
    body = progress.write_status(str(tmp_path), "render", "Rendering v1")
    status = json.loads((tmp_path / "status.json").read_text())
    assert status == body and status["stage"] == "render" and status["message"] == "Rendering v1"
    assert "T" in status["at"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["status.json"]     # no temp file left behind


def test_progress_replaces_by_rename_never_truncates(tmp_path, monkeypatch):
    progress.write_status(str(tmp_path), "analyze", "first")
    calls = []
    real = os.replace
    monkeypatch.setattr(progress.os, "replace", lambda a, b: (calls.append((a, b)), real(a, b)))
    progress.write_status(str(tmp_path), "graphics", "second")
    assert len(calls) == 1 and calls[0][1] == str(tmp_path / "status.json") and calls[0][0] != calls[0][1]
    assert json.loads((tmp_path / "status.json").read_text())["stage"] == "graphics"


def test_progress_cli_rejects_unknown_stage(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPTS / "progress.py"), "download", "x", "--job", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 2 and not (tmp_path / "status.json").exists()


def test_progress_cli_uses_job_dir_from_env(tmp_path):
    env = {**os.environ, "INFOCUS_JOB_DIR": str(tmp_path)}
    r = subprocess.run([sys.executable, str(SCRIPTS / "progress.py"), "check", "Verifying"], env=env)
    assert r.returncode == 0
    assert json.loads((tmp_path / "status.json").read_text())["message"] == "Verifying"


def test_example_result_json_validates_against_the_schema():
    jsonschema.validate(EXAMPLE_RESULT, SCHEMA)


def test_abort_result_validates():
    jsonschema.validate({"output": None, "poster": None, "decisions": [], "needsReview": True,
                         "checks": {"coversClean": False, "loudness": []},
                         "abort": "This doesn't look like a show recording: no anchor shot"}, SCHEMA)


@pytest.mark.parametrize("bad", [
    {**EXAMPLE_RESULT, "decisions": [{"kind": "mood", "detail": "x", "flagged": False}]},
    {k: v for k, v in EXAMPLE_RESULT.items() if k != "needsReview"},
    {**EXAMPLE_RESULT, "checks": {"loudness": []}},
])
def test_schema_rejects_malformed_results(bad):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, SCHEMA)


def test_skill_never_asks_and_names_the_contract():
    text = (SKILL / "SKILL.md").read_text()
    assert "AskUserQuestion" not in text
    for needle in ("brief.json", "result.json", "progress.py", "verify_render.py", "build_ffmpeg.py", "houseRules",
                   "abort"):
        assert needle in text, needle


def test_skill_keeps_graphics_and_anchors_as_they_are():
    """A real show came back with a half-size Follow card, then with the anchors recomposited at 86%, both to get
    past the studio check. The skill must say plainly that neither is ever done."""
    text = (SKILL / "SKILL.md").read_text()
    for needle in ("Overlapping the anchors is fine", "The anchors stay as recorded", "Never work around a check",
                   "reframe, rescale or recomposite the anchors", "resize, recolor, re-time, move or swap a package graphic",
                   "change how `build_ffmpeg.py` encodes"):
        assert needle in text, needle


def test_skill_needs_only_shell_and_files():
    """Claude, Codex or Grok may follow SKILL.md: no editing app, no MCP tools, no tool names of one agent."""
    for path in [SKILL / "SKILL.md", *(SKILL / "references").glob("*.md")]:
        text = path.read_text()
        for word in ("Resolve", "MCP", "mcp__", "run_script", "Skill tool", "slash command"):
            assert word not in text, (path.name, word)
    assert not (SCRIPTS / "build_resolve.py").exists()


def test_tracked_skill_files_hold_no_personal_paths():
    for path in SKILL.rglob("*"):
        if path.is_file() and path.suffix in {".md", ".py", ".mjs", ".json", ".swift"}:
            text = path.read_text()
            assert "/Users/" not in text and "~/Downloads" not in text and "roster.csv" not in text, path
