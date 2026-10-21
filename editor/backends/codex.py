"""Codex (ChatGPT subscription) through `codex exec --json`, in its own CODEX_HOME.

Flags checked against codex-cli 0.163.0 (`codex exec --help`, `codex exec resume --help`).
`resume` has no -C/--add-dir, so it runs with the repo as its working directory.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Callable

from editor.backends.base import AgentOutcome, BackendStatus, OnEvent, failure
from editor.backends.cli import agent_env, parse_json_line, private_dir, read_auth
from editor.backends.guard import codex_rules
from editor.backends.proc import ProcessRunner, run_process
from editor.config import Config
from editor.prompts import instruction

CONFIG_TOML = (
    "# Written by infocus-editor before every run; edits here are replaced.\n"
    "# No MCP servers: the editing agent uses shell and file tools only.\n"
)


class CodexBackend:
    name = "codex"
    label = "Codex"

    def __init__(self, cfg: Config, *, runner: ProcessRunner = run_process,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run, binary: str = "codex"):
        self._cfg = cfg
        self._runner = runner
        self._run = run
        self._bin = binary

    @property
    def home(self) -> Path:
        return self._cfg.codex_home

    def env(self) -> dict[str, str]:
        return agent_env({"CODEX_HOME": str(self.home)})

    def prepare_home(self) -> None:
        private_dir(self.home)
        (self.home / "config.toml").write_text(CONFIG_TOML)
        private_dir(self.home / "rules")
        (self.home / "rules" / "infocus.rules").write_text(codex_rules())

    def status(self) -> BackendStatus:
        if read_auth(self.home / "auth.json"):
            return BackendStatus(self.name, True, "Signed in with a ChatGPT account")
        return BackendStatus(self.name, False, "Not connected")

    def verify(self) -> str | None:
        try:
            proc = self._run([self._bin, "login", "status"], capture_output=True, text=True, check=False,
                             timeout=60, env=self.env())
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Couldn't run the Codex CLI to check the sign-in: {e.__class__.__name__}"
        return None if proc.returncode == 0 else "Codex says it isn't signed in; connect Codex again."

    def command(self, job_dir: Path, prompt: str, resume_id: str | None) -> list[str]:
        common = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        if resume_id:
            return [self._bin, "exec", "resume", *common, resume_id, prompt]
        dirs = []
        for d in (job_dir, self._cfg.package_dir, self._cfg.data_dir):
            dirs += ["--add-dir", str(d)]
        return [self._bin, "exec", *common, "-C", str(self._cfg.repo_root), *dirs, prompt]

    async def run(self, job_dir: Path, prompt: str, resume_id: str | None, on_event: OnEvent,
                  cancel: asyncio.Event) -> AgentOutcome:
        if not read_auth(self.home / "auth.json"):
            return AgentOutcome(ok=False, backend=self.name, session_id=None, auth_failed=True,
                                error="Codex isn't connected")
        self.prepare_home()
        state = _CodexState(resume_id)
        full_prompt = instruction(self._cfg.repo_root, prompt, with_rules=True)
        result = await self._runner(self.command(job_dir, full_prompt, resume_id), env=self.env(),
                                    cwd=self._cfg.repo_root, on_line=lambda l: state.feed(l, on_event),
                                    cancel=cancel)
        if result.cancelled or cancel.is_set():
            return failure(self.name, state.session_id, "cancelled", cancelled=True)
        if result.returncode == 0 and not state.error:
            return AgentOutcome(ok=True, backend=self.name, session_id=state.session_id)
        error = state.error or result.stderr_tail.strip()[-500:] or f"codex exited {result.returncode}"
        return failure(self.name, state.session_id, f"Codex: {error}")


class _CodexState:
    """Reads `codex exec --json` events: thread.started, item.*, turn.completed/failed, error."""

    def __init__(self, session_id: str | None):
        self.session_id = session_id
        self.error: str | None = None

    def feed(self, line: str, on_event: OnEvent) -> None:
        event = parse_json_line(line)
        if event is None:
            return
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            self.session_id = str(event["thread_id"])
        elif kind == "turn.failed":
            self.error = str((event.get("error") or {}).get("message") or "turn failed")
        elif kind == "error":
            self.error = str(event.get("message") or "error")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "command_execution":
                on_event("tool Bash " + str(item.get("command") or "")[:200])
            elif item.get("type") == "agent_message":
                on_event("say " + str(item.get("text") or "").replace("\n", " ")[:300])
        elif kind == "turn.completed":
            self.error = None
