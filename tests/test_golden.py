"""Golden runs: the real agent edits a real show end to end, with a fake Portal and Drive.

Run on the editing Mac with `uv run pytest -m golden -s` (about 15-30 min per case, uses
the connected AI subscription). Show data holds real names, so it lives outside git in
`<fixture>/golden/`:
  claim.json     the claim the Portal would send ("downloads" urls are local file paths)
  expected.json  the approved edl.json for that show, plus "intro_end" / "outro_start"
                 (switcher frames no source segment may cross)
The fixture dir is $INFOCUS_GOLDEN_FIXTURE (default ~/InFocusEditor/fixtures/2026-10-21).
AI sign-ins come from the data dir ($INFOCUS_GOLDEN_DATA, default ~/InFocusEditor) and the
Keychain; $GOLDEN_CLAUDE_OAUTH_TOKEN overrides the Claude one for a dev machine.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from editor.portal import parse_claim

pytestmark = pytest.mark.golden

FIXTURE = Path(os.environ.get("INFOCUS_GOLDEN_FIXTURE", "~/InFocusEditor/fixtures/2026-10-21")).expanduser()
DATA = Path(os.environ.get("INFOCUS_GOLDEN_DATA", "~/InFocusEditor")).expanduser()
PACKAGE = Path(os.environ.get("INFOCUS_PACKAGE", "~/InFocusEditor/package")).expanduser()
FRAME_SLACK = 15
PEAK_DBFS, PEAK_TOL = -6.0, 0.3


class FakePortal:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.progress_log: list[tuple[str, str]] = []
        self.completed: dict[str, Any] | None = None
        self.failed: str | None = None

    async def progress(self, job_id: str, stage: str, message: str) -> bool:
        self.progress_log.append((stage, message))
        return False

    async def upload_targets(self, job_id: str):
        from editor.portal import UploadTarget, UploadTargets
        return UploadTargets(str(self.out_dir), UploadTarget(self._video, "t"), UploadTarget(self._poster, "t"))

    async def complete(self, job_id: str, result: dict[str, Any]) -> None:
        self.completed = result

    async def fail(self, job_id: str, error: str, cancelled: bool = False) -> None:
        self.failed = error

    def expect(self, brief: dict[str, Any]) -> None:
        self._video, self._poster = brief["outputNasPath"], brief["posterNasPath"]


class LocalDrive:
    """Downloads are local paths; uploads land under the Portal's out_dir."""

    async def download(self, url: str, dest: Path, expected_size: int | None = None) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(url, dest)
        return dest

    async def upload(self, base_url: str, remote_path: str, token: str, local: Path) -> None:
        target = Path(base_url) / remote_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, target)


def _load(name: str) -> dict[str, Any]:
    path = FIXTURE / "golden" / name
    if not path.exists():
        pytest.skip(f"golden fixture missing: {path}")
    return json.loads(path.read_text())


def _deps(tmp_path: Path, portal: FakePortal):
    from editor.backends import BackendPool
    from editor.backends.claude import ClaudeBackend
    from editor.backends.codex import CodexBackend
    from editor.backends.grok import GrokBackend
    from editor.keychain import Keychain, MemoryKeychain
    from editor.runner import RunnerDeps
    from tests.helpers import make_config

    rules = tmp_path / "house-rules.md"
    shutil.copyfile(Path(__file__).parents[1] / "house-rules.example.md", rules)
    cfg = make_config(tmp_path, data_dir=DATA, work_root=tmp_path / "jobs", house_rules=rules,
                      package_dir=PACKAGE)
    token = os.environ.get("GOLDEN_CLAUDE_OAUTH_TOKEN")
    keychain = MemoryKeychain({"claude_oauth_token": token}) if token else Keychain()
    pool = BackendPool([ClaudeBackend(cfg, keychain), CodexBackend(cfg), GrokBackend(cfg)], cfg.agents)
    if not pool.connected():
        pytest.skip("no AI account connected for the golden run")
    return RunnerDeps(cfg=cfg, portal=portal, drive=LocalDrive(), pool=pool), cfg


async def _edit(tmp_path: Path, mutate=None):
    from editor.runner import run_job
    raw = _load("claim.json")
    if mutate:
        raw = mutate(copy.deepcopy(raw))
    claim = parse_claim(raw)
    portal = FakePortal(tmp_path / "nas")
    portal.expect(raw["brief"])
    deps, cfg = _deps(tmp_path, portal)
    report = await run_job(deps, claim)
    return report, portal, cfg, claim, raw


def _job_show_dir(cfg, claim) -> Path:
    dirs = sorted(cfg.work_root.glob(f"{claim.show_date}-{claim.job_id}*"))
    assert dirs, "job folder missing"
    return dirs[-1] / "show"


def _source_segments(edl: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in edl["segments"] if s.get("kind") == "source"}


def _assert_matches_expected(show_dir: Path, expected: dict[str, Any]) -> dict[str, Any]:
    edl = json.loads((show_dir / "edl.json").read_text())
    segs, want = edl["segments"], expected["segments"]
    assert [s.get("kind") for s in segs] == [s.get("kind") for s in want], "segment kinds/order differ"

    assert "Intro" in segs[0].get("path", ""), "first segment must be the package intro"
    assert "Outro" in segs[-1].get("path", ""), "last segment must be the package outro"

    got_src = [s for s in segs if s.get("kind") == "source"]
    want_src = [s for s in want if s.get("kind") == "source"]
    for g, w in zip(got_src, want_src):
        assert abs(g["in"] - w["in"]) <= FRAME_SLACK, f"{w['id']} in {g['in']} vs {w['in']}"
        assert abs(g["out"] - w["out"]) <= FRAME_SLACK, f"{w['id']} out {g['out']} vs {w['out']}"
        assert g.get("card") == w.get("card"), f"{w['id']} card text differs"
        assert g["in"] >= expected["intro_end"] and g["out"] <= expected["outro_start"]

    names = {(o.get("render") or {}).get(k) for o in edl.get("overlays", []) for k in ("name1", "name2", "name")}
    for o in expected["overlays"]:
        for k in ("name1", "name2", "name"):
            if (o.get("render") or {}).get(k):
                assert o["render"][k] in names, f"name {o['render'][k]} missing from graphics"
    assert len(edl.get("overlays", [])) >= len(expected["overlays"]), "fewer covers than the approved edit"
    return edl


def _assert_verified(show_dir: Path, portal: FakePortal) -> None:
    verify = json.loads((show_dir / "check" / "verify.json").read_text())
    assert verify["pass"], verify["failures"]
    for row in verify.get("loudness", []):
        assert abs(row["peak"] - PEAK_DBFS) <= PEAK_TOL, row
    assert portal.completed and portal.completed["checks"]["coversClean"] is True


async def test_golden_edit_matches_the_approved_cut(tmp_path):
    report, portal, cfg, claim, _ = await _edit(tmp_path)
    assert report.status == "REVIEW", portal.failed
    show_dir = _job_show_dir(cfg, claim)
    _assert_matches_expected(show_dir, _load("expected.json"))
    _assert_verified(show_dir, portal)
    assert not portal.completed["needsReview"], portal.completed["decisions"]


async def test_golden_remote_fix_changes_only_that_card(tmp_path):
    from editor.portal import parse_claim as parse
    from editor.runner import run_job
    report, portal, cfg, claim, raw = await _edit(tmp_path)
    assert report.status == "REVIEW", portal.failed
    show_dir = _job_show_dir(cfg, claim)
    before = json.loads((show_dir / "edl.json").read_text())

    fix_raw = copy.deepcopy(raw)
    fix_raw["kind"] = "fix"
    fix_raw.pop("downloads", None)
    fix_raw["job"]["version"] = fix_raw["brief"]["version"] = 2
    for key in ("outputNasPath", "posterNasPath"):
        fix_raw["brief"][key] = fix_raw["brief"][key].replace(".mp4", "-v2.mp4").replace(".poster", "-v2.poster")
    fix_raw["fix"] = {"text": "Make the Unity Day card say 'Wear Orange to School Tomorrow'",
                      "remember": False, "agentSessionId": portal.completed["agentSessionId"], "version": 2}
    portal.expect(fix_raw["brief"])
    deps, _ = _deps(tmp_path, portal)
    report = await run_job(deps, parse(fix_raw))
    assert report.status == "REVIEW", portal.failed

    after = json.loads((show_dir / "edl.json").read_text())
    changed = [b["id"] for b, a in zip(before["segments"], after["segments"]) if b != a]
    assert len(changed) == 1, f"fix changed {changed}"
    card = next(s for s in after["segments"] if s["id"] == changed[0])["card"]
    assert "Wear Orange to School Tomorrow" in card["points"]
    for b, a in zip(_source_segments(before).values(), _source_segments(after).values()):
        assert (b["in"], b["out"]) == (a["in"], a["out"]), "a fix must not move cut points"
    assert "card" in (portal.completed.get("reply") or "").lower()


async def test_golden_missing_package_becomes_a_flagged_placeholder(tmp_path):
    def drop_packages(raw):
        raw["brief"]["packages"] = []
        raw["downloads"] = [d for d in raw["downloads"] if d["path"] in raw["brief"]["sourcePaths"]]
        return raw

    report, portal, cfg, claim, _ = await _edit(tmp_path, drop_packages)
    assert report.status == "REVIEW", portal.failed
    edl = json.loads((_job_show_dir(cfg, claim) / "edl.json").read_text())
    assert any(s.get("kind") == "placeholder" for s in edl["segments"])
    assert portal.completed["needsReview"] is True
    assert any(d["kind"] == "package" and d["flagged"] for d in portal.completed["decisions"])
