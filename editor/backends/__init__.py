"""AI backends (Claude, Codex, Grok) behind one interface, plus the pool that
picks them in the configured order and falls back when one runs out."""
from __future__ import annotations

import time
from typing import Callable, Iterable, Sequence

from editor.backends.base import AgentOutcome, Backend, BackendStatus

__all__ = ["AgentOutcome", "Backend", "BackendStatus", "BackendPool", "parse_session_ref", "session_ref"]


def session_ref(backend: str, session_id: str | None) -> str:
    """How the Portal stores a session: `<backend>:<id>`."""
    return f"{backend}:{session_id}"[:100] if session_id else ""


def parse_session_ref(ref: str | None) -> tuple[str | None, str | None]:
    if not ref or ":" not in ref:
        return None, None
    backend, _, session_id = ref.partition(":")
    return (backend, session_id) if backend and session_id else (None, None)


class BackendPool:
    """Holds the backends in config order and what went wrong with each recently."""

    def __init__(self, backends: Sequence[Backend], order: Iterable[str],
                 clock: Callable[[], float] = time.time):
        self._all = {b.name: b for b in backends}
        self._backends = [self._all[n] for n in order if n in self._all]
        self._clock = clock
        self._problems: dict[str, str] = {}

    def set_order(self, order) -> None:
        """The Portal's settings.agentOrder (a subset and order of the backends) replaces config."""
        chosen = [self._all[n] for n in dict.fromkeys(order) if n in self._all]
        if chosen and chosen != self._backends:
            self._backends = chosen

    @property
    def names(self) -> list[str]:
        return [b.name for b in self._backends]

    def get(self, name: str) -> Backend | None:
        return next((b for b in self._backends if b.name == name), None)

    def connected(self) -> list[Backend]:
        """Signed-in backends in config order, minus any whose sign-in just failed."""
        return [b for b in self._backends if b.status().connected and b.name not in self._auth_failed()]

    def _auth_failed(self) -> set[str]:
        return {n for n, p in self._problems.items() if p.startswith("auth:")}

    def note(self, outcome: AgentOutcome) -> None:
        when = time.strftime("%H:%M", time.localtime(self._clock()))
        if outcome.auth_failed:
            self._problems[outcome.backend] = f"auth:Sign-in stopped working at {when}; connect it again"
        elif outcome.limit_hit:
            self._problems[outcome.backend] = f"limit:Hit its usage limit at {when}"

    def reset(self, name: str) -> None:
        """New credentials arrived for this backend."""
        self._problems.pop(name, None)

    def statuses(self) -> list[BackendStatus]:
        out = []
        for b in self._backends:
            status = b.status()
            problem = self._problems.get(b.name)
            if problem and status.connected:
                kind, _, text = problem.partition(":")
                status = BackendStatus(b.name, kind != "auth", text)
            out.append(status)
        return out

    def label(self, name: str) -> str:
        backend = self.get(name)
        return getattr(backend, "label", name.title()) if backend else name.title()
