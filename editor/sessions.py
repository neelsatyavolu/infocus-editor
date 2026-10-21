"""The agent part of a job: pick a backend, run sessions, verify with code,
resume with the failures, and fall back to the next backend on a usage limit
or a broken sign-in."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from editor.backends import BackendPool, parse_session_ref
from editor.jobfiles import JobPaths, read_json
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
    attempts = 0
    while True:
        async with forwarding(reporter, paths.status, deps.status_poll_s):
            outcome = await backend.run(paths.root, prompt, resume, lambda line, n=backend.name:
                                        log.info("%s: %s", n, line), cancel)
        cost += outcome.cost_usd
        if cancel.is_set() or outcome.cancelled:
            raise JobCancelled()
        if outcome.limit_hit or outcome.auth_failed:
            pool.note(outcome)
            if not queue:
                raise JobFailed(f"{pool.label(backend.name)} {_why(outcome)} and no other AI account is "
                                f"connected. Connect another one in the Portal, then Redo.")
            nxt = queue.pop(0)
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
        await reporter.report("check", "Checking the render")
        verdict = await asyncio.to_thread(deps.verify, deps.cfg.skill_dir, paths.show, Path(brief["output"]))
        if (result is not None and verdict.passed) or attempts == MAX_RESUMES:
            break
        attempts += 1
        retry = verify_retry_prompt(verdict.failures, result is None)
        prompt = retry if session else FALLBACK_PREFACE + first_prompt + "\n\n" + retry
        resume = session
    return SessionsResult(result, verdict, cost, backend.name, session, error, tuple(decisions))


def _decision(detail: str) -> dict[str, Any]:
    return {"kind": "backend", "detail": detail[:300], "flagged": False}
