"""Run one claimed job end to end: prepare, agent sessions (editor/sessions.py),
code verification, upload, report."""
from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from editor.backends import session_ref
from editor.config import Config
from editor.housekeeping import kill_job_processes
from editor.jobfiles import MAX_HOUSE_RULES, build_brief, job_paths, write_house_rules, write_json_atomic
from editor.media import MediaError, probe
from editor.portal import AuthError, Claim
from editor.power import job_awake
from editor.prepare import JobFailed, Prepared, prepare_edit, read_prepared, touch
from editor.progress import Reporter
from editor.recordings import profile_with_skill
from editor.sessions import JobCancelled, SessionsResult, run_sessions
from editor.verify import Verdict, run_verify

log = logging.getLogger(__name__)
MIN_RENDER_S = 120
MAX_RENDER_S = 15 * 60


@dataclass(frozen=True)
class RunnerDeps:
    cfg: Config
    portal: Any
    drive: Any
    pool: Any  # editor.backends.BackendPool
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    verify: Callable[..., Verdict] = run_verify
    sweep: Callable[[Path], Any] = kill_job_processes
    popen: Callable[..., subprocess.Popen] = subprocess.Popen
    envelope: Any = None  # audio envelope per file (editor.sync.audio_envelope unless a test supplies one)
    profile: Any = None  # picture profile per file (the skill's classifier unless a test supplies one)
    status_poll_s: float = 5.0


@dataclass(frozen=True)
class JobReport:
    status: str  # REVIEW | FAILED | CANCELLED
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


async def run_job(deps: RunnerDeps, claim: Claim, cancel: asyncio.Event | None = None) -> JobReport:
    with job_awake(deps.cfg.machine_kind, deps.popen):
        return await _run_job(deps, claim, cancel)


async def _run_job(deps: RunnerDeps, claim: Claim, cancel: asyncio.Event | None) -> JobReport:
    cancel = cancel or asyncio.Event()
    reporter = Reporter(deps.portal, claim.job_id, cancel)
    limit = deps.cfg.run_timeout_minutes
    try:
        payload = await asyncio.wait_for(_run(deps, claim, cancel, reporter), timeout=limit * 60)
        return JobReport("REVIEW", payload=payload)
    except asyncio.TimeoutError:
        return await _end(deps, claim, f"The edit took longer than {limit} minutes and was stopped "
                                       f"at the {reporter.stage} step.", sweep=True)
    except JobCancelled:
        return await _end(deps, claim, "Cancelled by a producer.", cancelled=True, sweep=True)
    except JobFailed as e:
        return await _end(deps, claim, str(e))
    except AuthError:
        raise
    except Exception as e:  # noqa: BLE001 - every other failure still reaches the Portal
        log.exception("job %s crashed", claim.job_id)
        return await _end(deps, claim, f"Editor error: {e.__class__.__name__}: {e}".splitlines()[0][:500],
                          sweep=True)


async def _end(deps: RunnerDeps, claim: Claim, message: str, *, cancelled: bool = False,
               sweep: bool = False) -> JobReport:
    if sweep:
        try:
            await asyncio.to_thread(deps.sweep, job_paths(deps.cfg, claim).root)
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't stop leftover job processes: %s", e)
    try:
        await deps.portal.fail(claim.job_id, message, cancelled=cancelled)
    except AuthError:
        raise
    except Exception as e:  # noqa: BLE001 - the Portal will requeue a stale job anyway
        log.error("couldn't report the failure to the Portal: %s", e)
    return JobReport("CANCELLED" if cancelled else "FAILED", message)


async def _run(deps: RunnerDeps, claim: Claim, cancel: asyncio.Event, reporter: Reporter) -> dict[str, Any]:
    cfg = deps.cfg
    paths = job_paths(cfg, claim)
    if claim.kind == "fix":
        prepared = read_prepared(paths) if paths.root.is_dir() else None
        if prepared is None:
            raise JobFailed("The edit files were cleaned up; use Redo.")
        touch(paths.root)
        await reporter.report("fix", f"Applying the fix for v{claim.version}")
    else:
        await reporter.report("download", "Downloading the recording and packages")
        prepared = await prepare_edit(deps.drive, claim, paths, deps.run, envelope=deps.envelope,
                                      profile=deps.profile or profile_with_skill(cfg.skill_dir / "scripts",
                                                                                 cfg.package_dir, deps.run))
    if cancel.is_set():
        raise JobCancelled()

    for stale in (paths.result, paths.status):
        stale.unlink(missing_ok=True)
    write_house_rules(cfg, claim, paths)
    brief = build_brief(cfg, claim, paths, prepared.recordings, prepared.source)
    write_json_atomic(paths.brief, brief)

    sessions = await run_sessions(deps, claim, paths, brief, cancel, reporter)
    result = sessions.result
    if result is None:
        why = f" ({sessions.last_error})" if sessions.last_error else ""
        raise JobFailed(f"The editor stopped without finishing the edit{why}.")
    output, poster = Path(brief["output"]), Path(brief["poster"])
    await asyncio.to_thread(check_output, output, poster, deps.run)

    payload = completion_payload(claim, result, sessions, prepared)
    if claim.fix and claim.fix.remember:
        # The Portal keeps the house rules; send back the rules the agent just extended.
        payload["houseRules"] = paths.house_rules.read_text()[:MAX_HOUSE_RULES]
    await reporter.report("upload", "Uploading the finished show")
    targets = await deps.portal.upload_targets(claim.job_id)
    if targets.video.path != payload["outputNasPath"]:
        raise JobFailed("The Portal gave an upload path that doesn't match the job.")
    await deps.drive.upload(targets.drive_base_url, targets.video.path, targets.video.token, output)
    await deps.drive.upload(targets.drive_base_url, targets.poster.path, targets.poster.token, poster)
    if cancel.is_set():
        raise JobCancelled()
    await deps.portal.complete(claim.job_id, payload)
    return payload


def check_output(output: Path, poster: Path, run=subprocess.run) -> None:
    if not output.is_file():
        raise JobFailed("The editor finished without a render.")
    try:
        info = probe(output, run)
    except MediaError as e:
        raise JobFailed(f"The render can't be read: {e}") from e
    if not info.has_video:
        raise JobFailed("The render has no video track.")
    if not info.has_audio:
        raise JobFailed("The render has no audio track.")
    if not MIN_RENDER_S <= info.duration <= MAX_RENDER_S:
        minutes, seconds = divmod(int(info.duration), 60)
        raise JobFailed(f"The render runs {minutes}:{seconds:02d}; a show should be 2–15 minutes.")
    if not poster.is_file():
        raise JobFailed("The editor finished without a poster frame.")


def completion_payload(claim: Claim, result: dict[str, Any], sessions: SessionsResult,
                       prepared: Prepared | None = None) -> dict[str, Any]:
    verdict = sessions.verdict
    early = list(prepared.decisions) if prepared else []  # skips, detected structure, weak matches
    decisions = early + list(sessions.decisions) + [
        {"kind": str(d.get("kind") or "other")[:40], "detail": str(d.get("detail") or "")[:300],
         "flagged": bool(d.get("flagged"))}
        for d in (result.get("decisions") or []) if isinstance(d, dict)
    ]
    decisions = decisions[:50]
    checks = dict(result.get("checks") or {})
    checks["coversClean"] = verdict.passed
    checks["failures"] = list(verdict.failures)
    if verdict.loudness:
        checks["loudness"] = list(verdict.loudness)
    needs_review = (bool(result.get("needsReview")) or not verdict.passed or any(d["flagged"] for d in decisions)
                    or bool(prepared and prepared.needs_review))
    payload: dict[str, Any] = {
        "outputNasPath": str(claim.brief.get("outputNasPath") or ""),
        "decisions": decisions,
        "checks": checks,
        "needsReview": needs_review,
        "costUsd": round(sessions.cost_usd, 4),
        "agentSessionId": session_ref(sessions.backend or "", sessions.session_id),
    }
    if claim.kind == "fix":
        reply = str(result.get("reply") or "").strip() or f"Applied the change and rebuilt as v{claim.version}."
        payload["reply"] = reply[:1000]
    return payload
