"""What every AI backend shares: the outcome, the status, the instruction
and how to tell a usage limit or a broken sign-in from an ordinary failure."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

OnEvent = Callable[[str], None]

LIMIT_RE = re.compile(
    r"usage[ _]limit|rate[ _]limit|quota|too many requests|\b429\b|limit reached|hit your .*limit|"
    r"out of (?:credits|messages)|insufficient[_ ]quota",
    re.IGNORECASE,
)
AUTH_RE = re.compile(
    r"\b401\b|\b403\b|unauthori[sz]ed|not (?:logged|signed) in|log ?in again|sign ?in again|"
    r"authentication|invalid (?:api key|token|grant|credentials)|token (?:has )?expired|refresh token|"
    r"oauth token",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AgentOutcome:
    ok: bool
    backend: str
    session_id: str | None
    cost_usd: float = 0.0
    error: str | None = None
    limit_hit: bool = False
    auth_failed: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class BackendStatus:
    name: str
    connected: bool
    detail: str

    def as_json(self) -> dict:
        return {"name": self.name, "connected": self.connected, "detail": self.detail[:200]}


class Backend(Protocol):
    name: str
    label: str

    def status(self) -> BackendStatus: ...

    def verify(self) -> str | None:
        """None when the sign-in works, else a plain reason (may cost a tiny request)."""

    async def run(self, job_dir: Path, prompt: str, resume_id: str | None, on_event: OnEvent,
                  cancel: asyncio.Event) -> AgentOutcome: ...


def classify(text: str) -> tuple[bool, bool]:
    """(limit_hit, auth_failed) for an error message."""
    if not text:
        return False, False
    if LIMIT_RE.search(text):
        return True, False
    return False, bool(AUTH_RE.search(text))


def failure(backend: str, session_id: str | None, error: str, *, cancelled: bool = False,
            cost_usd: float = 0.0) -> AgentOutcome:
    limit, auth = (False, False) if cancelled else classify(error)
    return AgentOutcome(ok=False, backend=backend, session_id=session_id, cost_usd=cost_usd,
                        error=error[:500], limit_hit=limit, auth_failed=auth, cancelled=cancelled)
