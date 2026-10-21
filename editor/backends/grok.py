"""Grok (SuperGrok subscription) through headless `grok -p`, in its own GROK_HOME.

Flags checked against grok 1.0.66 (`grok --help`): -p, --output-format streaming-json,
--cwd, --permission-mode bypassPermissions, --max-turns, -s/--session-id, -r/--resume,
--deny (deny rules still apply under bypassPermissions), --disable-web-search.
"""
from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from editor.backends.base import AgentOutcome, BackendStatus, OnEvent, failure
from editor.backends.cli import agent_env, parse_json_line, private_dir, read_auth
from editor.backends.guard import grok_deny_rules
from editor.backends.proc import ProcessRunner, run_process
from editor.config import Config
from editor.prompts import instruction


class GrokBackend:
    name = "grok"
    label = "Grok"

    def __init__(self, cfg: Config, *, runner: ProcessRunner = run_process,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run, binary: str = "grok",
                 new_id: Callable[[], str] = lambda: str(uuid.uuid4())):
        self._cfg = cfg
        self._runner = runner
        self._run = run
        self._bin = binary
        self._new_id = new_id

    @property
    def home(self) -> Path:
        return self._cfg.grok_home

    def env(self) -> dict[str, str]:
        return agent_env({"GROK_HOME": str(self.home)})

    def status(self) -> BackendStatus:
        if read_auth(self.home / "auth.json"):
            return BackendStatus(self.name, True, "Signed in with a Grok account")
        return BackendStatus(self.name, False, "Not connected")

    def verify(self) -> str | None:
        if not read_auth(self.home / "auth.json"):
            return "The Grok sign-in file is missing or unreadable."
        try:
            # Lists the models for the signed-in account and exits; fails without a working sign-in.
            proc = self._run([self._bin, "models"], capture_output=True, text=True, check=False,
                             timeout=60, env=self.env())
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Couldn't run the Grok CLI to check the sign-in: {e.__class__.__name__}"
        return None if proc.returncode == 0 else "Grok rejected the sign-in; connect Grok again."

    def command(self, prompt: str, session_id: str, resume: bool) -> list[str]:
        cmd = [self._bin, "-p", prompt, "--output-format", "streaming-json", "--cwd", str(self._cfg.repo_root),
               "--permission-mode", "bypassPermissions", "--max-turns", str(self._cfg.max_turns),
               "--disable-web-search"]
        cmd += ["--resume", session_id] if resume else ["-s", session_id]
        for rule in grok_deny_rules():
            cmd += ["--deny", rule]
        return cmd

    async def run(self, job_dir: Path, prompt: str, resume_id: str | None, on_event: OnEvent,
                  cancel: asyncio.Event) -> AgentOutcome:
        if not read_auth(self.home / "auth.json"):
            return AgentOutcome(ok=False, backend=self.name, session_id=None, auth_failed=True,
                                error="Grok isn't connected")
        private_dir(self.home)
        session_id = resume_id or self._new_id()
        state = _GrokState(session_id)
        task = (f"{prompt}\n\nThe job folder is {job_dir}; the graphics package is in {self._cfg.package_dir}.")
        full_prompt = instruction(self._cfg.repo_root, task, with_rules=True)
        result = await self._runner(self.command(full_prompt, session_id, bool(resume_id)), env=self.env(),
                                    cwd=self._cfg.repo_root, on_line=lambda l: state.feed(l, on_event),
                                    cancel=cancel)
        if result.cancelled or cancel.is_set() or state.stop_reason == "cancelled":
            return failure(self.name, state.session_id, "cancelled", cancelled=True)
        ok_stop = state.stop_reason in (None, "end_turn")
        if result.returncode == 0 and not state.error and ok_stop:
            return AgentOutcome(ok=True, backend=self.name, session_id=state.session_id, cost_usd=state.cost)
        error = (state.error or (f"stopped: {state.stop_reason}" if not ok_stop else None)
                 or result.stderr_tail.strip()[-500:] or f"grok exited {result.returncode}")
        return failure(self.name, state.session_id, f"Grok: {error}", cost_usd=state.cost)


class _GrokState:
    """Reads `--output-format streaming-json`: text, tool_call, usage, end, error."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.error: str | None = None
        self.stop_reason: str | None = None
        self.cost = 0.0

    def feed(self, line: str, on_event: OnEvent) -> None:
        event = parse_json_line(line)
        if event is None:
            return
        kind = event.get("type")
        if kind == "end":
            self.session_id = str(event.get("sessionId") or self.session_id)
            self.stop_reason = event.get("stopReason")
            cost = event.get("total_cost_usd")
            self.cost = float(cost) if isinstance(cost, (int, float)) else 0.0
        elif kind == "error":
            self.error = str(event.get("message") or "error")
        elif kind == "tool_call":
            on_event(f"tool {event.get('toolName') or event.get('title') or '?'}")
        elif kind == "text" and str(event.get("data") or "").strip():
            on_event("say " + str(event["data"]).replace("\n", " ")[:300])
