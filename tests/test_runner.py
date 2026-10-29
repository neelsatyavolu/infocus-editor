import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from editor.backends import AgentOutcome, BackendPool, BackendStatus
from editor.jobfiles import write_json_atomic
from editor.portal import Claim, Download, Fix, UploadTarget, UploadTargets
from editor.recordings import Profile
from editor.runner import RunnerDeps, run_job
from editor.verify import Verdict
from tests.helpers import make_config

DATE = "2026-10-21"
OUT_NAS = f"Package Storage/Shows/{DATE}/InFocus Show {DATE}-111.mp4"
POSTER_NAS = f"Package Storage/Shows/{DATE}/InFocus Show {DATE}-111.poster.jpg"
LOG_NAS = f"Package Storage/Shows/{DATE}/editor-logs/j1-v1-111.tar.gz"


def probe_json(duration=480.0, width=1920, height=1080, audio=True, video=True):
    streams = []
    if video:
        streams.append({"codec_type": "video", "codec_name": "h264", "width": width, "height": height,
                        "r_frame_rate": "60000/1001"})
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
    """An older Portal by default (no debug log target); `log_target=True` adds one."""

    def __init__(self, cancel_on=None, log_target=False):
        self.progress_calls = []
        self.completed = None
        self.failed = None
        self.debug_log_path = None
        self.calls = []
        self.cancel_on = cancel_on
        self.log_target = log_target

    async def progress(self, job_id, stage, message):
        self.progress_calls.append((stage, message))
        return self.cancel_on is not None and self.cancel_on == stage

    async def upload_targets(self, job_id):
        self.calls.append("upload_targets")
        return UploadTargets("https://drive.example.org", UploadTarget(OUT_NAS, "rw.v"),
                             UploadTarget(POSTER_NAS, "rw.p"),
                             UploadTarget(LOG_NAS, "rw.l") if self.log_target else None)

    async def complete(self, job_id, payload):
        self.calls.append("complete")
        self.completed = payload

    async def fail(self, job_id, error, cancelled=False, debug_log_path=None):
        self.calls.append("fail")
        self.failed = (error, cancelled)
        self.debug_log_path = debug_log_path


class FakeDrive:
    def __init__(self, fail_paths=()):
        self.downloads = []
        self.uploads = []
        self.fail_paths = set(fail_paths)

    async def download(self, url, dest, size):
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = (url.encode() * ((size or 1) // max(1, len(url)) + 1))[: size or 1]  # distinct per file
        dest.write_bytes(body)
        self.downloads.append((url, dest.name))
        return dest

    async def upload(self, base, path, token, local):
        assert local.is_file()
        if path in self.fail_paths:
            raise OSError("Drive upload failed (503)")
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


def deps_for(tmp_path, agent, *, portal=None, verdicts=None, run=None, timeout=75, kind="dedicated", drive=None):
    cfg = make_config(tmp_path, run_timeout_minutes=timeout, machine_kind=kind)
    verdicts = list(verdicts or [])

    def verify(skill_dir, show_dir, render):
        return verdicts.pop(0) if verdicts else Verdict(True, (), ())

    agents = agent if isinstance(agent, list) else [agent]
    pool = BackendPool(agents, ["claude", "codex", "grok"])
    return RunnerDeps(cfg=cfg, portal=portal or FakePortal(), drive=drive or FakeDrive(), pool=pool,
                      run=run or FakeRun(), verify=verify, sweep=Sweep(), popen=FakePopen, status_poll_s=0.01,
                      envelope=lambda path: None, profile=lambda path: Profile(()))


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
    assert Path(brief["source"]).name == "01.mov" and Path(brief["recordings"]).name == "recordings.json"


async def test_cards_go_to_the_portal_and_past_cards_reach_the_brief(tmp_path):
    def finish_with_cards(job_dir, cancel):
        write_json_atomic(job_dir / "show" / "edl.json", {"segments": [
            {"id": "open", "kind": "source"},
            {"id": "fair", "kind": "source", "card": {"title": "Club|Fair", "points": ["Friday", "Quad"], "script": 0}},
            {"id": "pies", "kind": "source", "card": {"title": "Pie Sale", "points": ["$25"], "script": 9}},
            {"id": "pkg", "kind": "file", "path": str(job_dir / "packages" / "01-pkg0.mp4"),
             "toss_said": "The club fair is Friday. Our reporter has more."}]})
        finish(job_dir)

    claim = edit_claim()
    past = [{"showDate": "2026-10-06", "title": "College Fair", "points": ["Monday"], "script": None}]
    submitted = [{"text": "Scan the QR to join!", "mediaLink": "https://drive.google.com/file/d/X/view", "moreInfo": None}]
    claim = Claim(claim.kind, claim.job_id, claim.show_date, claim.version,
                  {**claim.brief, "pastCards": past, "submissions": submitted}, claim.downloads)
    deps = deps_for(tmp_path, FakeAgent(finish_with_cards))
    await run_job(deps, claim)
    assert deps.portal.completed["checks"]["cards"] == [
        {"title": "Club Fair", "points": ["Friday", "Quad"], "script": "Club fair Friday"},
        {"title": "Pie Sale", "points": ["$25"], "script": None}]
    assert deps.portal.completed["checks"]["tosses"] == ["The club fair is Friday. Our reporter has more."]
    brief = json.loads((deps.cfg.work_root / f"{DATE}-j1" / "brief.json").read_text())
    assert brief["pastCards"] == past
    assert brief["submissions"] == submitted


@pytest.mark.parametrize("given, expected", [(True, True), (None, False)])
async def test_recomposite_reaches_the_brief(tmp_path, given, expected):
    claim = edit_claim()
    brief = {**claim.brief, **({"recomposite": given} if given is not None else {})}
    claim = Claim(claim.kind, claim.job_id, claim.show_date, claim.version, brief, claim.downloads)
    deps = deps_for(tmp_path, FakeAgent(finish))
    await run_job(deps, claim)
    written = json.loads((deps.cfg.work_root / f"{DATE}-j1" / "brief.json").read_text())
    assert written["recomposite"] is expected


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


async def test_two_takes_become_two_recordings_and_are_never_joined(tmp_path):
    run = FakeRun()
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)), run=run)
    await run_job(deps, edit_claim(sources=("Shows/part1.mov", "Shows/part2.mov")))
    job_dir = deps.cfg.work_root / f"{DATE}-j1"
    brief = json.loads((job_dir / "brief.json").read_text())
    recordings = json.loads(Path(brief["recordings"]).read_text())
    assert [(r["id"], r["kind"], [Path(f["path"]).name for f in r["files"]]) for r in recordings["recordings"]] == [
        ("r1", "program", ["01.mov"]), ("r2", "program", ["02.mov"])]
    assert brief["source"] is None  # more than one file: the skill reads recordings.json
    assert not (job_dir / "sources.txt").exists() and not any(c[0] == "ffmpeg" for c in run.commands)
    structure = [d for d in deps.portal.completed["decisions"] if d["kind"] == "structure"]
    assert structure == [{"kind": "structure", "detail": "2 recordings: r1 program, r2 program", "flagged": False}]


@pytest.mark.parametrize("probe,message", [
    (probe_json(40), "doesn't look like a show recording (it runs 0:40)"),
    (probe_json(480, width=720, height=480), "This is the SD capture; pick the HD recording."),
])
async def test_bad_recordings_fail_before_any_agent_cost(tmp_path, probe, message):
    agent = FakeAgent()
    deps = deps_for(tmp_path, agent, run=FakeRun({"01.mov": probe}))
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
    assert {"kind": "backend", "flagged": False,
            "detail": "Edited with Codex because Claude hit its usage limit"} in done["decisions"]
    status = {s.name: s for s in deps.pool.statuses()}
    assert status["claude"].connected and "usage limit" in status["claude"].detail


async def test_broken_sign_in_falls_back_and_marks_the_backend_disconnected(tmp_path):
    claude = FakeAgent(lambda d, c: AgentOutcome(ok=False, backend="claude", session_id=None, auth_failed=True,
                                                 error="401 unauthorized"))
    grok = FakeAgent(lambda d, c: finish(d), name="grok")
    deps = deps_for(tmp_path, [claude, grok])
    await run_job(deps, edit_claim())
    backend = [d for d in deps.portal.completed["decisions"] if d["kind"] == "backend"]
    assert "Edited with Grok because Claude needs signing in again" in backend[0]["detail"]
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


# ---- job log and debug bundle ---------------------------------------------------------


def job_log_text(deps):
    return (deps.cfg.work_root / f"{DATE}-j1" / "logs" / "editor.log").read_text()


def bundle_names(deps, version=1):
    import tarfile

    with tarfile.open(deps.cfg.work_root / f"{DATE}-j1" / "logs" / f"debug-{version}.tar.gz") as tar:
        return tar.getnames()


async def test_the_job_log_records_every_step_and_a_header_per_run(tmp_path):
    import logging

    handlers = list(logging.getLogger().handlers)
    bad = Verdict(False, ({"rule": "cover_span", "overlay": "dual", "detail": "ends early"},))
    deps = deps_for(tmp_path, FakeAgent(*[lambda d, c: finish(d, statuses=[("render", "Rendering")])] * 2),
                    verdicts=[bad, Verdict(True)])
    await run_job(deps, edit_claim())
    deps2 = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d, reply="Done")))
    await run_job(deps2, fix_claim())
    assert logging.getLogger().handlers == handlers  # detached after each job
    text = job_log_text(deps)
    for expected in [
        "----- run 1 (edit, v1) -----",
        "claim: job j1, show 2026-10-21, edit v1, 1 source(s), 1 package(s), 2 download(s)",
        "precheck: 1 source(s) (0 ignored), 1 package(s), 2 download(s) sent",
        "download Shows/show.mov -> ", "MB/s)",
        "recording r1: program", "detection: structure: 1 recording: r1 program",
        "precheck: length 4:00 is within 1-30 minutes",
        "brief.json written: ", "progress: download: Downloading the recording and packages",
        "session 1: claude, fresh", "session 1 ended: ok, session sess-1, turns ?, cost $1.5000",
        "progress: render: Rendering", "result.json: 0 decision(s) (0 flagged), needsReview=False",
        "verify_render: fail (cover_span), report ", "show/check/verify.json",
        "verify failure: {\"rule\": \"cover_span\"",
        "verify resume 1/2: 1 failure(s); resuming session sess-1",
        "session 2: claude, resuming sess-1", "verify_render: pass, report",
        "output check: ", "272.6 s, video h264 1920x1080, audio aac, poster present",
        f"upload {deps.cfg.work_root / f'{DATE}-j1' / 'out'}", f"-> {OUT_NAS}: 0.0 MB in",
        "completing: needsReview=False, coversClean=True",
        "the Portal has no debug log target (older Portal)",
        "complete sent, debugLogPath none",
        "----- run 2 (fix, v2) -----", "claim: job j1, show 2026-10-21, fix v2", "precheck: the fix reuses",
        "reply: Done",
    ]:
        assert expected in text, expected
    assert "https://drive.example.org/f?path" not in text  # download URLs never reach the log
    assert text.index("----- run 1") < text.index("----- run 2")


async def test_complete_carries_the_uploaded_debug_bundle(tmp_path):
    portal = FakePortal(log_target=True)
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)), portal=portal)
    report = await run_job(deps, edit_claim())
    assert report.status == "REVIEW"
    assert deps.drive.uploads[2] == (LOG_NAS, "rw.l", "debug-1.tar.gz")
    assert portal.completed["debugLogPath"] == LOG_NAS
    assert portal.calls == ["upload_targets", "complete"]  # one set of targets serves all three uploads
    assert {"logs/editor.log", "brief.json", "result.json", "show/recordings.json"} <= set(bundle_names(deps))


async def test_a_failed_run_uploads_the_bundle_before_failing(tmp_path):
    portal = FakePortal(log_target=True)
    run = FakeRun({f"InFocus Show {DATE} v1.mp4": probe_json(272.6, audio=False)})
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)), portal=portal, run=run)
    report = await run_job(deps, edit_claim())
    assert report.status == "FAILED"
    assert portal.calls == ["upload_targets", "fail"]
    assert portal.failed == ("The render has no audio track.", False) and portal.debug_log_path == LOG_NAS
    assert deps.drive.uploads == [(LOG_NAS, "rw.l", "debug-1.tar.gz")]
    assert "failing the job (cancelled=False): The render has no audio track." in job_log_text(deps)


async def test_a_cancelled_run_still_sends_its_bundle(tmp_path):
    async def step(job_dir, cancel):
        write_json_atomic(job_dir / "status.json", {"stage": "render", "message": "Rendering"})
        for _ in range(200):
            if cancel.is_set():
                return AgentOutcome(ok=False, backend="claude", session_id="s", cancelled=True)
            await asyncio.sleep(0.01)

    portal = FakePortal(cancel_on="render", log_target=True)
    deps = deps_for(tmp_path, FakeAgent(step), portal=portal)
    report = await run_job(deps, edit_claim())
    assert report.status == "CANCELLED" and portal.failed == ("Cancelled by a producer.", True)
    assert portal.debug_log_path == LOG_NAS
    assert "cancelled by a producer at the render step" in job_log_text(deps)


async def test_a_timed_out_run_logs_the_stage_and_sends_its_bundle(tmp_path):
    async def step(job_dir, cancel):
        write_json_atomic(job_dir / "status.json", {"stage": "render", "message": "Rendering"})
        await asyncio.sleep(10)

    portal = FakePortal(log_target=True)
    deps = deps_for(tmp_path, FakeAgent(step), portal=portal, timeout=0.005)
    await run_job(deps, edit_claim())
    assert portal.debug_log_path == LOG_NAS
    assert "timed out: the 0.005-minute limit ran out at the render step" in job_log_text(deps)


async def test_a_crash_logs_the_traceback_and_sends_its_bundle(tmp_path):
    def step(job_dir, cancel):
        raise KeyError("brief")

    portal = FakePortal(log_target=True)
    deps = deps_for(tmp_path, FakeAgent(step), portal=portal)
    report = await run_job(deps, edit_claim())
    assert report.status == "FAILED" and report.message.startswith("Editor error: KeyError")
    text = job_log_text(deps)
    assert "Traceback (most recent call last)" in text and "KeyError: 'brief'" in text
    assert portal.debug_log_path == LOG_NAS


async def test_a_failed_bundle_upload_still_completes(tmp_path):
    portal = FakePortal(log_target=True)
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)), portal=portal, drive=FakeDrive([LOG_NAS]))
    report = await run_job(deps, edit_claim())
    assert report.status == "REVIEW" and portal.completed is not None
    assert "debugLogPath" not in portal.completed
    assert "couldn't upload the debug bundle: OSError: Drive upload failed (503)" in job_log_text(deps)


async def test_a_failed_bundle_upload_still_fails_the_job(tmp_path):
    portal = FakePortal(log_target=True)
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d, poster=False)), portal=portal,
                    drive=FakeDrive([LOG_NAS]))
    report = await run_job(deps, edit_claim())
    assert report.status == "FAILED" and portal.failed[0] == "The editor finished without a poster frame."
    assert portal.debug_log_path is None


async def test_an_older_portal_gets_no_bundle_upload(tmp_path):
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)))
    await run_job(deps, edit_claim())
    assert [u[0] for u in deps.drive.uploads] == [OUT_NAS, POSTER_NAS]
    assert "debugLogPath" not in deps.portal.completed
    assert "logs/editor.log" in bundle_names(deps)  # still built locally
    deps2 = deps_for(tmp_path / "other", FakeAgent())
    await run_job(deps2, fix_claim())  # fails: no job folder on this machine
    assert deps2.portal.failed[0] == "The edit files were cleaned up; use Redo."
    assert deps2.portal.debug_log_path is None and deps2.drive.uploads == []


async def test_a_cancel_during_the_bundle_upload_cancels_instead_of_completing(tmp_path):
    cancel = asyncio.Event()

    class CancellingDrive(FakeDrive):
        async def upload(self, base, path, token, local):
            await super().upload(base, path, token, local)
            if path == LOG_NAS:
                cancel.set()

    portal = FakePortal(log_target=True)
    deps = deps_for(tmp_path, FakeAgent(lambda d, c: finish(d)), portal=portal, drive=CancellingDrive())
    report = await run_job(deps, edit_claim(), cancel)
    assert report.status == "CANCELLED" and portal.completed is None
    assert portal.failed == ("Cancelled by a producer.", True)


def test_chapters_follow_the_finished_show(tmp_path):
    from editor.jobfiles import edl_chapters
    pkg = str(tmp_path / "packages" / "01-pkg.mp4")
    segs = [("intro", "file", {"path": "{PACKAGE}/03 Intro & Outro/InFocus Intro 2026.mp4"}), ("open", "source", {}),
            ("karaoke", "source", {"card": {"title": "Karaoke|Club"}}), ("karaoke_b", "source", {"card": {"title": "Karaoke|Club"}}),
            ("bdtw", "source", {"card": {"title": "Breaking Down Walls"}}), ("toss", "source", {}),
            ("pkg", "file", {"path": pkg}), ("close", "source", {}), ("outro", "file", {"path": "x/InFocus Outro 2026.mp4"})]
    write_json_atomic(tmp_path / "edl.json", {"fps": "60000/1001", "segments": [{"id": i, "kind": k, **x} for i, k, x in segs]})
    frames = [0, 600, 1800, 2400, 3000, 4200, 4800, 15600, 18000]
    write_json_atomic(tmp_path / "layout.json", {"fps": "60000/1001", "segments": [
        {"id": i, "kind": k, "rec_in": f, "rec_out": f + 1} for (i, k, _), f in zip(segs, frames)]})
    chapters = edl_chapters(tmp_path / "edl.json", tmp_path / "layout.json", [{"localPath": pkg, "title": "Field trip lawsuit"}])
    assert [c["title"] for c in chapters] == [
        "Intro", "Welcome", "Karaoke Club", "Breaking Down Walls", "Package: Field trip lawsuit", "Sign-off", "Outro"]
    assert chapters[0]["start"] == 0 and chapters[2]["start"] == round(1800 / (60000 / 1001), 2)


def test_a_chapter_named_by_the_skill_wins(tmp_path):
    from editor.jobfiles import edl_chapters
    write_json_atomic(tmp_path / "edl.json", {"fps": "60000/1001", "segments": [
        {"id": "open", "kind": "source", "chapter": "Good morning, Paly"}, {"id": "sports", "kind": "source", "chapter": "Sports"}]})
    write_json_atomic(tmp_path / "layout.json", {"fps": "60000/1001", "segments": [
        {"id": "open", "rec_in": 0}, {"id": "sports", "rec_in": 1200}]})
    assert [c["title"] for c in edl_chapters(tmp_path / "edl.json", tmp_path / "layout.json", [])] == ["Good morning, Paly", "Sports"]
