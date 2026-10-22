"""Run one claimed job end to end: prepare, agent sessions (editor/sessions.py),
code verification, upload, report."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from editor.backends import session_ref
from editor.config import Config
from editor.debugbundle import build_bundle
from editor.housekeeping import kill_job_processes
from editor.jobfiles import (MAX_HOUSE_RULES, build_brief, job_paths, read_json, write_house_rules,
                             write_json_atomic)
from editor.joblog import describe_claim, describe_payload, describe_recordings, job_log
from editor.media import MediaError, probe
from editor.portal import AuthError, Claim, UploadTarget, UploadTargets
from editor.power import job_awake
from editor.prepare import JobFailed, Prepared, prepare_edit, read_prepared, touch
from editor.progress import Reporter
from editor.recordings import profile_with_skill
from editor.sessions import JobCancelled, SessionsResult, run_sessions
from editor.verify import Verdict, run_verify

log = logging.getLogger(__name__)
MIN_RENDER_S = 120
MAX_RENDER_S = 15 * 60
BUNDLE_TIMEOUT_S = 5 * 60


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
    with job_log(job_paths(deps.cfg, claim).root, claim.kind, claim.version), \
            job_awake(deps.cfg.machine_kind, deps.popen):
        log.info("%s", describe_claim(claim))
        return await _run_job(deps, claim, cancel)


async def _run_job(deps: RunnerDeps, claim: Claim, cancel: asyncio.Event | None) -> JobReport:
    cancel = cancel or asyncio.Event()
    reporter = Reporter(deps.portal, claim.job_id, cancel)
    limit = deps.cfg.run_timeout_minutes
    try:
        payload, targets = await asyncio.wait_for(_run(deps, claim, cancel, reporter), timeout=limit * 60)
        # Outside the time limit: an edit that's uploaded never fails because the debug bundle was slow.
        return JobReport("REVIEW", payload=await _complete(deps, claim, payload, targets, cancel))
    except asyncio.TimeoutError:
        log.error("timed out: the %s-minute limit ran out at the %s step", limit, reporter.stage)
        return await _end(deps, claim, f"The edit took longer than {limit} minutes and was stopped "
                                       f"at the {reporter.stage} step.", sweep=True)
    except JobCancelled:
        log.warning("cancelled by a producer at the %s step", reporter.stage)
        return await _end(deps, claim, "Cancelled by a producer.", cancelled=True, sweep=True)
    except JobFailed as e:
        return await _end(deps, claim, str(e))
    except AuthError:
        log.error("the Portal rejected the editor token; the job stops without reporting")
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
    log.info("failing the job (cancelled=%s): %s", cancelled, message)
    debug_path = await _send_bundle(deps, claim)
    try:
        await deps.portal.fail(claim.job_id, message, cancelled=cancelled, debug_log_path=debug_path)
        log.info("fail sent, debugLogPath %s", debug_path or "none")
    except AuthError:
        raise
    except Exception as e:  # noqa: BLE001 - the Portal will requeue a stale job anyway
        log.error("couldn't report the failure to the Portal: %s", e)
    return JobReport("CANCELLED" if cancelled else "FAILED", message)


async def _send_bundle(deps: RunnerDeps, claim: Claim, targets: UploadTargets | None = None) -> str | None:
    """Build the debug bundle and upload it to the Portal's log target, within BUNDLE_TIMEOUT_S.
    Returns its NAS path, or None (no log target on an older Portal, or any failure)."""
    try:
        return await asyncio.wait_for(_bundle_and_upload(deps, claim, targets), BUNDLE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - the job still completes or fails without it
        log.warning("couldn't upload the debug bundle: %s: %s", e.__class__.__name__, e)
        return None


async def _bundle_and_upload(deps: RunnerDeps, claim: Claim, targets: UploadTargets | None) -> str | None:
    bundle = await asyncio.to_thread(build_bundle, job_paths(deps.cfg, claim).root, claim.version)
    if targets is None:
        targets = await deps.portal.upload_targets(claim.job_id)
    if targets.log is None:
        log.info("the Portal has no debug log target (older Portal); the bundle stays at %s", bundle)
        return None
    await _upload(deps, targets.drive_base_url, targets.log, bundle)
    return targets.log.path


async def _upload(deps: RunnerDeps, base_url: str, target: UploadTarget, local: Path) -> None:
    size = local.stat().st_size
    started = time.monotonic()
    await deps.drive.upload(base_url, target.path, target.token, local)
    log.info("upload %s -> %s: %.1f MB in %.1f s", local, target.path, size / 1e6, time.monotonic() - started)


async def _run(deps: RunnerDeps, claim: Claim, cancel: asyncio.Event, reporter: Reporter
               ) -> tuple[dict[str, Any], UploadTargets]:
    """Everything up to and including the uploads; returns the completion payload."""
    cfg = deps.cfg
    paths = job_paths(cfg, claim)
    if claim.kind == "fix":
        prepared = read_prepared(paths) if paths.root.is_dir() else None
        if prepared is None:
            raise JobFailed("The edit files were cleaned up; use Redo.")
        log.info("precheck: the fix reuses %s (recordings %s)", paths.root, prepared.recordings)
        touch(paths.root)
        await reporter.report("fix", f"Applying the fix for v{claim.version}")
    else:
        await reporter.report("download", "Downloading the recording and packages")
        prepared = await prepare_edit(deps.drive, claim, paths, deps.run, envelope=deps.envelope,
                                      profile=deps.profile or profile_with_skill(cfg.skill_dir / "scripts",
                                                                                 cfg.package_dir, deps.run))
        _log_prepared(prepared)
    if cancel.is_set():
        raise JobCancelled()

    for stale in (paths.result, paths.status):
        stale.unlink(missing_ok=True)
    write_house_rules(cfg, claim, paths)
    brief = build_brief(cfg, claim, paths, prepared.recordings, prepared.source)
    write_json_atomic(paths.brief, brief)
    log.info("brief.json written: %s (%s v%s, output %s)", paths.brief, claim.kind, claim.version, brief["output"])

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
    await _upload(deps, targets.drive_base_url, targets.video, output)
    await _upload(deps, targets.drive_base_url, targets.poster, poster)
    return payload, targets


async def _complete(deps: RunnerDeps, claim: Claim, payload: dict[str, Any], targets: UploadTargets,
                    cancel: asyncio.Event) -> dict[str, Any]:
    if cancel.is_set():
        raise JobCancelled()
    log.info("completing: %s", describe_payload(payload))
    debug_path = await _send_bundle(deps, claim, targets)
    if cancel.is_set():  # a cancel that arrived while the bundle went up
        raise JobCancelled()
    if debug_path:
        payload = {**payload, "debugLogPath": debug_path}
    await deps.portal.complete(claim.job_id, payload)
    log.info("complete sent, debugLogPath %s", debug_path or "none")
    return payload


def _log_prepared(prepared: Prepared) -> None:
    for line in describe_recordings(read_json(prepared.recordings) or {}):
        log.info("%s", line)
    for d in prepared.decisions:
        log.info("detection: %s%s: %s", d.get("kind"), " (flagged)" if d.get("flagged") else "", d.get("detail"))
    if prepared.needs_review:
        log.info("detection: the edit will need review")


def check_output(output: Path, poster: Path, run=subprocess.run) -> None:
    if not output.is_file():
        raise JobFailed("The editor finished without a render.")
    try:
        info = probe(output, run)
    except MediaError as e:
        raise JobFailed(f"The render can't be read: {e}") from e
    log.info("output check: %s: %.1f s, video %s %dx%d, audio %s, poster %s", output, info.duration,
             info.video_codec or "none", info.width, info.height, info.audio_codec or "none",
             "present" if poster.is_file() else "missing")
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
