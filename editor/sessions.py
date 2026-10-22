"""The agent part of a job: pick a backend, run sessions, verify with code,
resume with the failures, and fall back to the next backend on a usage limit
or a broken sign-in."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from editor.backends import BackendPool, parse_session_ref
from editor.jobfiles import JobPaths, read_json
from editor.joblog import clip
from editor.portal import Claim
from editor.prepare import JobFailed
from editor.progress import Reporter, forwarding
from editor.prompts import FALLBACK_PREFACE, edit_prompt, fix_prompt, verify_retry_prompt
from editor.verify import Verdict

log = logging.getLogger(__name__)
MAX_RESUMES = 2
NO_BACKEND = "No AI account connected: in the Portal open Automatic editing → Connect AI account."


class JobCancelled(Exception):
    pass


@dataclass(frozen=True)
class SessionsResult:
    result: dict[str, Any] | None
    verdict: Verdict
    cost_usd: float
    backend: str | None
    session_id: str | None
    last_error: str | None = None
    decisions: tuple[dict[str, Any], ...] = field(default=())


def _why(outcome) -> str:
    return "hit its usage limit" if outcome.limit_hit else "needs signing in again"


async def run_sessions(deps, claim: Claim, paths: JobPaths, brief: dict[str, Any], cancel: asyncio.Event,
                       reporter: Reporter) -> SessionsResult:
    pool: BackendPool = deps.pool
    queue = pool.connected()
    if not queue:
        raise JobFailed(NO_BACKEND)
    decisions: list[dict[str, Any]] = []
    if claim.fix:
        first_prompt = fix_prompt(paths.root, claim.version - 1, claim.fix.text, claim.fix.remember)
        wanted, resume = parse_session_ref(claim.fix.agent_session_id)
        match = next((b for b in queue if b.name == wanted), None)
        if match:
            queue = [match] + [b for b in queue if b is not match]
        else:
            if wanted:
                decisions.append(_decision(f"Fixed with {pool.label(queue[0].name)} in a fresh session because "
                                           f"{pool.label(wanted)} isn't connected"))
            resume = None
            first_prompt = FALLBACK_PREFACE + first_prompt
    else:
        first_prompt = edit_prompt(paths.root, brief.get("note"))
        resume = None

    backend = queue.pop(0)
    prompt = first_prompt
    cost, session, result, verdict, error = 0.0, None, None, Verdict(False), None
    attempts = number = 0
    while True:
        number += 1
        log.info("session %d: %s, %s", number, backend.name, f"resuming {resume}" if resume else "fresh")
        started = time.monotonic()
        async with forwarding(reporter, paths.status, deps.status_poll_s):
            outcome = await backend.run(paths.root, prompt, resume, lambda line, n=backend.name:
                                        log.debug("%s: %s", n, line), cancel)
        _log_outcome(number, outcome, time.monotonic() - started)
        cost += outcome.cost_usd
        if cancel.is_set() or outcome.cancelled:
            raise JobCancelled()
        if outcome.limit_hit or outcome.auth_failed:
            pool.note(outcome)
            if not queue:
                raise JobFailed(f"{pool.label(backend.name)} {_why(outcome)} and no other AI account is "
                                f"connected. Connect another one in the Portal, then Redo.")
            nxt = queue.pop(0)
            log.warning("%s %s; switching to %s in a fresh session", backend.name, _why(outcome), nxt.name)
            decisions.append(_decision(f"Edited with {pool.label(nxt.name)} because {pool.label(backend.name)} "
                                       f"{_why(outcome)}"))
            backend, resume, session = nxt, None, None
            prompt = FALLBACK_PREFACE + first_prompt
            continue
        session = outcome.session_id or session
        error = outcome.error
        result = read_json(paths.result)
        if result and result.get("abort"):
            raise JobFailed(str(result["abort"])[:1000])
        log.info("result.json: %s", _describe_result(result))
        await reporter.report("check", "Checking the render")
        verdict = await asyncio.to_thread(deps.verify, deps.cfg.skill_dir, paths.show, Path(brief["output"]))
        _log_verdict(verdict, paths.show / "check" / "verify.json")
        if (result is not None and verdict.passed) or attempts == MAX_RESUMES:
            break
        attempts += 1
        retry = verify_retry_prompt(verdict.failures, result is None)
        prompt = retry if session else FALLBACK_PREFACE + first_prompt + "\n\n" + retry
        resume = session
        log.info("verify resume %d/%d: %s; %s", attempts, MAX_RESUMES,
                 "result.json is missing" if result is None else f"{len(verdict.failures)} failure(s)",
                 f"resuming session {session}" if session else "no session to resume, starting fresh")
    return SessionsResult(result, verdict, cost, backend.name, session, error, tuple(decisions))


def _log_outcome(number: int, outcome, seconds: float) -> None:
    status = "ok" if outcome.ok else f"error: {outcome.error}"
    log.info("session %d ended: %s, session %s, turns %s, cost $%.4f, limit_hit=%s, auth_failed=%s, "
             "cancelled=%s, %.1f s", number, status, outcome.session_id or "none",
             "?" if outcome.turns is None else outcome.turns, outcome.cost_usd, outcome.limit_hit,
             outcome.auth_failed, outcome.cancelled, seconds)


def _describe_result(result: dict[str, Any] | None) -> str:
    if result is None:
        return "missing"
    decisions = [d for d in result.get("decisions") or [] if isinstance(d, dict)]
    return (f"{len(decisions)} decision(s) ({sum(1 for d in decisions if d.get('flagged'))} flagged), "
            f"needsReview={result.get('needsReview')}, abort={result.get('abort')!r}")


def _log_verdict(verdict: Verdict, report: Path) -> None:
    rules = sorted({str(f.get("rule")) for f in verdict.failures if isinstance(f, dict)})
    log.info("verify_render: %s%s, report %s", "pass" if verdict.passed else "fail",
             f" ({', '.join(rules)})" if rules else "", report)
    for failure in verdict.failures:
        log.debug("verify failure: %s", clip(failure))


def _decision(detail: str) -> dict[str, Any]:
    return {"kind": "backend", "detail": detail[:300], "flagged": False}
