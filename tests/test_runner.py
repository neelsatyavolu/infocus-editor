import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from editor.backends import AgentOutcome, BackendPool, BackendStatus
from editor.jobfiles import write_json_atomic
from editor.portal import Claim, Download, Fix, UploadTarget, UploadTargets
from editor.runner import RunnerDeps, run_job
from editor.verify import Verdict
from tests.helpers import make_config

DATE = "2026-10-21"
OUT_NAS = f"Package Storage/Shows/{DATE}/InFocus Show {DATE}-111.mp4"
POSTER_NAS = f"Package Storage/Shows/{DATE}/InFocus Show {DATE}-111.poster.jpg"


def probe_json(duration=480.0, width=1920, height=1080, audio=True, video=True):
    streams = []
    if video:
        streams.append({"codec_type": "video", "codec_name": "h264", "width": width, "height": height})
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return json.dumps({"format": {"duration": str(duration)}, "streams": streams})


class FakeRun:
    """Stands in for subprocess.run: ffprobe answers by file name, ffmpeg writes its output."""

    def __init__(self, probes=None):
        self.probes = {"source.mov": probe_json(), "source.mp4": probe_json(),
                       f"InFocus Show {DATE} v1.mp4": probe_json(272.6),
                       f"InFocus Show {DATE} v2.mp4": probe_json(272.6),
                       "01.mov": probe_json(240), "02.mov": probe_json(240)}
        self.probes.update(probes or {})
        self.commands = []

    def __call__(self, cmd, **kw):
        self.commands.append(cmd)
        if cmd[0] == "ffprobe":
            name = Path(cmd[-1]).name
            return subprocess.CompletedProcess(cmd, 0, stdout=self.probes.get(name, probe_json()), stderr="")
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"joined")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git":
            return subprocess.run(cmd, **kw)
        raise AssertionError(f"unexpected command {cmd}")


class FakePortal:
    def __init__(self, cancel_on=None):
        self.progress_calls = []
        self.completed = None
        self.failed = None
        self.cancel_on = cancel_on

    async def progress(self, job_id, stage, message):
        self.progress_calls.append((stage, message))
        return self.cancel_on is not None and self.cancel_on == stage

    async def upload_targets(self, job_id):
        return UploadTargets("https://drive.example.org", UploadTarget(OUT_NAS, "rw.v"),
                             UploadTarget(POSTER_NAS, "rw.p"))

    async def complete(self, job_id, payload):
        self.completed = payload

    async def fail(self, job_id, error, cancelled=False):
        self.failed = (error, cancelled)


class FakeDrive:
    def __init__(self):
        self.downloads = []
        self.uploads = []

    async def download(self, url, dest, size):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"m" * (size or 1))
        self.downloads.append((url, dest.name))
        return dest

    async def upload(self, base, path, token, local):
        assert local.is_file()
        self.uploads.append((path, token, local.name))


class Sweep:
    def __init__(self):
        self.dirs = []

    def __call__(self, job_dir):
        self.dirs.append(job_dir)


def finish(job_dir: Path, *, reply=None, decisions=(), needs_review=False, statuses=(), poster=True):
    """What a successful skill run leaves behind."""
    brief = json.loads((job_dir / "brief.json").read_text())
    for stage, message in statuses:
        write_json_atomic(job_dir / "status.json", {"stage": stage, "message": message})
    out = Path(brief["output"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"render")
    if poster:
        Path(brief["poster"]).write_bytes(b"jpeg")
    write_json_atomic(job_dir / "result.json", {
        "output": str(out), "poster": brief["poster"], "decisions": list(decisions),
        "checks": {"coversClean": True, "loudness": [{"segment": "open", "lufs": -19.5, "peak": -6.0, "ok": True}]},
        "needsReview": needs_review, "reply": reply, "abort": None})


class FakeAgent:
    """A backend whose sessions run scripted steps."""

    def __init__(self, *steps, name="claude", connected=True):
        self.name = name
        self.label = name.title()
        self.connected = connected
        self.steps = list(steps)
        self.calls = []

    def status(self):
        return BackendStatus(self.name, self.connected, "test")

    async def run(self, job_dir, prompt, resume_id, on_event, cancel):
        self.calls.append({"job_dir": job_dir, "prompt": prompt, "resume": resume_id})
        step = self.steps.pop(0)
        out = step(job_dir, cancel)
        if asyncio.iscoroutine(out):
            out = await out
        return out or AgentOutcome(ok=True, backend=self.name, cost_usd=1.5, session_id="sess-1")


def edit_claim(sources=("Shows/show.mov",), packages=1, version=1):
    pkgs = [{"title": f"Pkg {i}", "nasPath": f"Package Storage/Final/pkg{i}.mp4", "toss": None}
            for i in range(packages)]
    downloads = tuple(Download(p, f"https://drive.example.org/f?path={i}", 100) for i, p in enumerate(sources)) + \
        tuple(Download(p["nasPath"], f"https://drive.example.org/p{i}", 50) for i, p in enumerate(pkgs))
    brief = {"jobId": "j1", "showDate": DATE, "filmedOn": "2026-10-20", "version": version,
             "sourcePaths": list(sources), "anchors": ["Abby Example", "Otto Example"],
             "announcements": ["Club fair Friday"], "packages": pkgs, "note": None,
             "outputNasPath": OUT_NAS, "posterNasPath": POSTER_NAS}
    return Claim("edit", "j1", DATE, version, brief, downloads)


def fix_claim(remember=False, session="claude:sess-1"):
    base = edit_claim(version=2)
    return Claim("fix", "j1", DATE, 2, base.brief, (),
                 Fix("Make the card say Tomorrow", remember, session, 2))


class FakePopen:
    started = []

    def __init__(self, cmd, **kw):
        FakePopen.started.append(cmd)
        self.terminated = False

    def terminate(self):
        self.terminated = True
        FakePopen.started.append("terminated")

    def wait(self, timeout=None):
        return 0


def deps_for(tmp_path, agent, *, portal=None, verdicts=None, run=None, timeout=75, kind="dedicated"):
    cfg = make_config(tmp_path, run_timeout_minutes=timeout, machine_kind=kind)
    verdicts = list(verdicts or [])

    def verify(skill_dir, show_dir, render):
        return verdicts.pop(0) if verdicts else Verdict(True, (), ())

    agents = agent if isinstance(agent, list) else [agent]
    pool = BackendPool(agents, ["claude", "codex", "grok"])
    return RunnerDeps(cfg=cfg, portal=portal or FakePortal(), drive=FakeDrive(), pool=pool,
                      run=run or FakeRun(), verify=verify, sweep=Sweep(), popen=FakePopen, status_poll_s=0.01)


async def test_happy_path_uploads_to_the_brief_paths_and_completes(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    report = await run_job(deps, edit_claim())
    assert report.status == "REVIEW"
    assert deps.drive.uploads == [(OUT_NAS, "rw.v", f"InFocus Show {DATE} v1.mp4"),
                                  (POSTER_NAS, "rw.p", f"InFocus Show {DATE} v1.poster.jpg")]
    done = deps.portal.completed
    assert done["outputNasPath"] == OUT_NAS and done["needsReview"] is False
    assert done["checks"]["coversClean"] is True and done["costUsd"] == 1.5
    assert done["agentSessionId"] == "claude:sess-1" and "reply" not in done
    job_dir = deps.cfg.work_root / f"{DATE}-j1"
    brief = json.loads((job_dir / "brief.json").read_text())
    assert brief["anchors"] == ["Abby Example", "Otto Example"]
    assert brief["packages"][0]["localPath"].endswith("packages/01-pkg0.mp4")
    assert Path(brief["source"]).name == "source.mov"


async def test_status_json_changes_are_forwarded_once_each(tmp_path):
    async def step(job_dir, cancel):
        for stage, msg in [("analyze", "Analyzing"), ("analyze", "Analyzing"), ("render", "Rendering")]:
            write_json_atomic(job_dir / "status.json", {"stage": stage, "message": msg})
            await asyncio.sleep(0.05)
        finish(job_dir)

    deps = deps_for(tmp_path, FakeAgent(step))
    await run_job(deps, edit_claim())
    calls = deps.portal.progress_calls
    assert calls.count(("analyze", "Analyzing")) == 1 and calls.count(("render", "Rendering")) == 1
    assert calls[0][0] == "download" and calls[-1][0] == "upload"


async def test_missing_result_json_resumes_the_session_with_the_failure_prompt(tmp_path):
    agent = FakeAgent(lambda d, c: None, lambda d, c: finish(d))
    deps = deps_for(tmp_path, agent, verdicts=[Verdict(False, ({"rule": "render", "detail": "No render"},))])
    report = await run_job(deps, edit_claim())
    assert report.status == "REVIEW"
    assert agent.calls[1]["resume"] == "sess-1"
    assert "result.json is missing" in agent.calls[1]["prompt"]


async def test_missing_result_json_after_all_resumes_fails_the_job(tmp_path):
    agent = FakeAgent(*[lambda d, c: None] * 3)
    deps = deps_for(tmp_path, agent, verdicts=[Verdict(False)] * 3)
    report = await run_job(deps, edit_claim())
    assert report.status == "FAILED" and len(agent.calls) == 3
    assert "stopped without finishing" in deps.portal.failed[0]


async def test_verification_failures_are_resumed_at_most_twice_then_complete_with_needs_review(tmp_path):
    bad = Verdict(False, ({"rule": "cover_span", "overlay": "dual", "frames": [1212, 1713], "detail": "ends early"},))
    agent = FakeAgent(*[lambda d, c: finish(d)] * 3)
    deps = deps_for(tmp_path, agent, verdicts=[bad, bad, bad])
    report = await run_job(deps, edit_claim())
    assert report.status == "REVIEW" and len(agent.calls) == 3
    assert "cover_span" in agent.calls[1]["prompt"] and "cover_span" in agent.calls[2]["prompt"]
    done = deps.portal.completed
    assert done["needsReview"] is True and done["checks"]["coversClean"] is False
    assert done["checks"]["failures"][0]["rule"] == "cover_span"
    assert done["costUsd"] == 4.5


async def test_flagged_decision_sets_needs_review(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(
        d, decisions=[{"kind": "package", "detail": "placeholder for missing package", "flagged": True}])))
    await run_job(deps, edit_claim())
    assert deps.portal.completed["needsReview"] is True


async def test_video_without_audio_fails_the_job(tmp_path):
    run = FakeRun({f"InFocus Show {DATE} v1.mp4": probe_json(272.6, audio=False)})
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)), run=run)
    report = await run_job(deps, edit_claim())
    assert report.status == "FAILED" and deps.portal.failed == ("The render has no audio track.", False)
    assert deps.drive.uploads == []


async def test_missing_poster_fails_the_job(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d, poster=False)))
    report = await run_job(deps, edit_claim())
    assert "poster" in report.message


async def test_cancel_interrupts_the_session_and_reports_cancelled(tmp_path):
    async def step(job_dir, cancel):
        write_json_atomic(job_dir / "status.json", {"stage": "render", "message": "Rendering"})
        for _ in range(200):
            if cancel.is_set():
                return AgentOutcome(ok=False, backend="claude", cost_usd=0.4, session_id="s", cancelled=True)
            await asyncio.sleep(0.01)
        raise AssertionError("never cancelled")

    deps = deps_for(tmp_path, FakeAgent(step), portal=FakePortal(cancel_on="render"))
    report = await run_job(deps, edit_claim())
    assert report.status == "CANCELLED"
    assert deps.portal.failed == ("Cancelled by a producer.", True)
    assert len(deps.sweep.dirs) == 1 and deps.portal.completed is None


async def test_cancel_from_the_heartbeat_event(tmp_path):
    cancel = asyncio.Event()

    async def step(job_dir, ev):
        cancel.set()
        return AgentOutcome(ok=False, backend="claude", cost_usd=0, session_id="s", cancelled=True)

    deps = deps_for(tmp_path, FakeAgent(step))
    report = await run_job(deps, edit_claim(), cancel)
    assert report.status == "CANCELLED"


async def test_wall_clock_limit_fails_the_job_with_the_current_stage(tmp_path):
    async def step(job_dir, cancel):
        write_json_atomic(job_dir / "status.json", {"stage": "render", "message": "Rendering"})
        await asyncio.sleep(10)

    deps = deps_for(tmp_path, FakeAgent(step), timeout=0.005)  # 0.3 s
    report = await run_job(deps, edit_claim())
    assert report.status == "FAILED"
    assert "longer than" in report.message and "render step" in report.message
    assert deps.sweep.dirs == [deps.cfg.work_root / f"{DATE}-j1"]


async def test_fix_resumes_the_session_in_the_existing_job_dir_and_completes_with_the_reply(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    await run_job(deps, edit_claim())
    agent = FakeAgent(lambda d, c: finish(d, reply="Changed the Unity Day card, rebuilt as v2."))
    deps2 = deps_for(tmp_path, agent)
    report = await run_job(deps2, fix_claim())
    assert report.status == "REVIEW"
    assert agent.calls[0]["resume"] == "sess-1"
    assert agent.calls[0]["job_dir"] == deps.cfg.work_root / f"{DATE}-j1"
    assert "<producer_request>" in agent.calls[0]["prompt"]
    assert deps2.drive.downloads == []
    assert deps2.portal.completed["reply"] == "Changed the Unity Day card, rebuilt as v2."
    assert deps2.drive.uploads[0][2] == f"InFocus Show {DATE} v2.mp4"
    brief = json.loads((deps.cfg.work_root / f"{DATE}-j1" / "brief.json").read_text())
    assert brief["version"] == 2 and brief["fix"]["text"] == "Make the card say Tomorrow"


async def test_fix_with_a_missing_job_dir_fails_with_the_redo_message(tmp_path):
    deps = deps_for(tmp_path, FakeAgent())
    report = await run_job(deps, fix_claim())
    assert deps.portal.failed == ("The edit files were cleaned up; use Redo.", False)
    assert report.status == "FAILED"


async def test_remembered_fix_sends_the_updated_house_rules(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    await run_job(deps, edit_claim())
    base = fix_claim(remember=True)
    claim = Claim(base.kind, base.job_id, base.show_date, base.version,
                  {**base.brief, "houseRules": "1. Replace every graphic.\n"}, base.downloads, base.fix)

    def fix_step(job_dir, cancel):
        brief = json.loads((job_dir / "brief.json").read_text())
        rules = Path(brief["houseRules"])
        rules.write_text(rules.read_text() + "2. Announcement cards say Tomorrow for next-day events.\n")
        finish(job_dir, reply="Done")

    deps2 = deps_for(tmp_path, FakeAgent(fix_step))
    await run_job(deps2, claim)
    assert deps2.portal.completed["houseRules"] == (
        "1. Replace every graphic.\n2. Announcement cards say Tomorrow for next-day events.\n")
    assert not (deps.cfg.data_dir / ".git").exists()


async def test_two_files_are_joined_in_order_before_analysis(tmp_path):
    run = FakeRun()
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)), run=run)
    await run_job(deps, edit_claim(sources=("Shows/part1.mov", "Shows/part2.mov")))
    job_dir = deps.cfg.work_root / f"{DATE}-j1"
    listing = (job_dir / "sources.txt").read_text().splitlines()
    assert listing[0].endswith("src/01.mov'") and listing[1].endswith("src/02.mov'")
    concat = [c for c in run.commands if c[0] == "ffmpeg"][0]
    assert "-c" in concat and concat[concat.index("-c") + 1] == "copy"


async def test_mismatched_files_are_reencoded_to_prores(tmp_path):
    run = FakeRun({"02.mov": probe_json(240, width=1280, height=720)})
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)), run=run)
    await run_job(deps, edit_claim(sources=("Shows/a.mov", "Shows/b.mov")))
    concat = [c for c in run.commands if c[0] == "ffmpeg"][0]
    assert "prores_ks" in concat


@pytest.mark.parametrize("probe,message", [
    (probe_json(40), "doesn't look like a show recording (it runs 0:40)"),
    (probe_json(480, width=720, height=480), "This is the SD capture; pick the HD recording."),
])
async def test_bad_recordings_fail_before_any_agent_cost(tmp_path, probe, message):
    agent = FakeAgent()
    deps = deps_for(tmp_path, agent, run=FakeRun({"source.mov": probe}))
    report = await run_job(deps, edit_claim())
    assert report.status == "FAILED" and message in deps.portal.failed[0]
    assert agent.calls == []


async def test_abort_in_result_fails_the_job(tmp_path):
    def step(job_dir, cancel):
        write_json_atomic(job_dir / "result.json", {"abort": "This doesn't look like a show recording: no anchor shot"})

    deps = deps_for(tmp_path, FakeAgent(step))
    report = await run_job(deps, edit_claim())
    assert deps.portal.failed[0] == "This doesn't look like a show recording: no anchor shot"
    assert report.status == "FAILED"


async def test_completed_downloads_are_not_fetched_again(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    job_dir = deps.cfg.work_root / f"{DATE}-j1"
    (job_dir / "src").mkdir(parents=True)
    (job_dir / "src" / "01.mov").write_bytes(b"m" * 100)
    await run_job(deps, edit_claim())
    assert [name for _, name in deps.drive.downloads] == ["01-pkg0.mp4"]


def test_run_verify_reads_verify_json(tmp_path):
    from editor.verify import run_verify, verify_command

    show = tmp_path / "show"
    (show / "check").mkdir(parents=True)
    render = tmp_path / "out.mp4"
    render.write_bytes(b"x")

    def fake(cmd, **kw):
        assert cmd[-4].endswith("verify_render.py") and cmd[-1] == str(render)
        (show / "check" / "verify.json").write_text(json.dumps(
            {"pass": False, "failures": [{"rule": "audio", "segment": "throw", "detail": "peak -7.7"}],
             "loudness": [{"segment": "throw", "peak": -7.7, "ok": False}]}))
        return subprocess.CompletedProcess(cmd, 1, "", "")

    v = run_verify(Path("/skill"), show, render, run=fake)
    assert not v.passed and v.failures[0]["rule"] == "audio" and v.loudness[0]["ok"] is False
    assert verify_command(Path("/s"), show, render)[:2] == ["uv", "run"]


def test_run_verify_without_a_report_fails_with_the_exit_code(tmp_path):
    from editor.verify import run_verify

    render = tmp_path / "out.mp4"
    render.write_bytes(b"x")
    v = run_verify(Path("/skill"), tmp_path / "show", render,
                   run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, "", "Traceback: boom"))
    assert not v.passed and "exited 2" in v.failures[0]["detail"]


def test_cleanup_removes_only_old_job_dirs(tmp_path):
    import os
    import time

    from editor.housekeeping import cleanup_job_dirs

    old, new = tmp_path / "2026-10-01-a", tmp_path / "2026-10-20-b"
    old.mkdir()
    new.mkdir()
    now = time.time()
    os.utime(old, (now - 15 * 86400, now - 15 * 86400))
    assert cleanup_job_dirs(tmp_path, 14, now) == [old]
    assert new.exists() and not old.exists()


def limit(name):
    return lambda d, c: AgentOutcome(ok=False, backend=name, session_id="x", limit_hit=True,
                                     error="You've hit your usage limit")


async def test_usage_limit_falls_back_to_the_next_backend_in_a_fresh_session(tmp_path):
    claude = FakeAgent(limit("claude"))
    codex = FakeAgent(lambda d, c: finish(d), name="codex")
    deps = deps_for(tmp_path, [codex, claude])  # order comes from config, not this list
    report = await run_job(deps, edit_claim())
    assert report.status == "REVIEW"
    assert len(claude.calls) == 1 and codex.calls[0]["resume"] is None
    assert codex.calls[0]["prompt"].startswith("This continues an earlier edit")
    done = deps.portal.completed
    assert done["agentSessionId"] == "codex:sess-1"
    assert done["decisions"][0] == {"kind": "backend", "flagged": False,
                                    "detail": "Edited with Codex because Claude hit its usage limit"}
    status = {s.name: s for s in deps.pool.statuses()}
    assert status["claude"].connected and "usage limit" in status["claude"].detail


async def test_broken_sign_in_falls_back_and_marks_the_backend_disconnected(tmp_path):
    claude = FakeAgent(lambda d, c: AgentOutcome(ok=False, backend="claude", session_id=None, auth_failed=True,
                                                 error="401 unauthorized"))
    grok = FakeAgent(lambda d, c: finish(d), name="grok")
    deps = deps_for(tmp_path, [claude, grok])
    await run_job(deps, edit_claim())
    assert "Edited with Grok because Claude needs signing in again" in deps.portal.completed["decisions"][0]["detail"]
    assert [b.name for b in deps.pool.connected()] == ["grok"]


async def test_limit_with_no_other_backend_fails_the_job(tmp_path):
    deps = deps_for(tmp_path, [FakeAgent(limit("claude")), FakeAgent(name="codex", connected=False)])
    report = await run_job(deps, edit_claim())
    assert report.status == "FAILED" and "no other AI account is connected" in report.message


async def test_no_connected_backend_fails_with_the_connect_hint(tmp_path):
    deps = deps_for(tmp_path, [FakeAgent(connected=False)])
    report = await run_job(deps, edit_claim())
    assert report.message.startswith("No AI account connected")


async def test_fix_with_a_null_session_starts_fresh(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    await run_job(deps, edit_claim())
    agent = FakeAgent(lambda d, c: finish(d, reply="Done"))
    report = await run_job(deps_for(tmp_path, agent), fix_claim(session=None))
    assert report.status == "REVIEW"
    assert agent.calls[0]["resume"] is None
    assert agent.calls[0]["prompt"].startswith("This continues an earlier edit")


async def test_fix_resumes_on_the_backend_that_made_the_edit(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    await run_job(deps, edit_claim())
    claude = FakeAgent()
    codex = FakeAgent(lambda d, c: finish(d, reply="Done"), name="codex")
    deps2 = deps_for(tmp_path, [claude, codex])
    await run_job(deps2, fix_claim(session="codex:thread-9"))
    assert claude.calls == [] and codex.calls[0]["resume"] == "thread-9"
    assert deps2.portal.completed["agentSessionId"] == "codex:sess-1"


async def test_fix_from_a_disconnected_backend_runs_fresh_on_the_next_one(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    await run_job(deps, edit_claim())
    codex = FakeAgent(lambda d, c: finish(d, reply="Done"), name="codex")
    deps2 = deps_for(tmp_path, [FakeAgent(connected=False), codex])
    await run_job(deps2, fix_claim(session="claude:sess-1"))
    assert codex.calls[0]["resume"] is None
    assert deps2.portal.completed["decisions"][0]["detail"] == (
        "Fixed with Codex in a fresh session because Claude isn't connected")


async def test_personal_machine_keeps_awake_only_during_the_job(tmp_path):
    import os

    FakePopen.started = []
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)), kind="personal")
    await run_job(deps, edit_claim())
    assert FakePopen.started == [["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())], "terminated"]


async def test_dedicated_machine_starts_no_caffeinate(tmp_path):
    FakePopen.started = []
    await run_job(deps_for(tmp_path, FakeAgent(lambda d, c: finish(d))), edit_claim())
    assert FakePopen.started == []


async def test_house_rules_come_from_the_brief(tmp_path):
    claim = edit_claim()
    claim = Claim(claim.kind, claim.job_id, claim.show_date, claim.version,
                  {**claim.brief, "houseRules": "1. The Portal's own rules.\n"}, claim.downloads)
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    await run_job(deps, claim)
    job_dir = deps.cfg.work_root / f"{DATE}-j1"
    brief = json.loads((job_dir / "brief.json").read_text())
    assert brief["houseRules"] == str(job_dir / "house-rules.md")
    assert (job_dir / "house-rules.md").read_text() == "1. The Portal's own rules.\n"
    assert "houseRules" not in deps.portal.completed


async def test_missing_house_rules_use_the_built_in_example(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    await run_job(deps, edit_claim())
    text = (deps.cfg.work_root / f"{DATE}-j1" / "house-rules.md").read_text()
    assert text == deps.cfg.house_rules_example.read_text()
