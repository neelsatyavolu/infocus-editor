"""Codex (ChatGPT subscription) through `codex exec --json`, in its own CODEX_HOME.

Flags checked against codex-cli 0.163.0 (`codex exec --help`, `codex exec resume --help`).
`resume` has no -C/--add-dir, so it runs with the repo as its working directory.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, Callable

from editor.backends.base import EFFORT, AgentOutcome, BackendStatus, OnEvent, failure
from editor.backends.cli import agent_env, parse_json_line, private_dir, read_auth
from editor.backends.guard import codex_rules
from editor.backends.proc import ProcessRunner, run_process
from editor.config import Config
from editor.joblog import Transcript, clip
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
        common = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                  "-c", f'model_reasoning_effort="{EFFORT}"']
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
        with Transcript(job_dir, self.name) as transcript:
            result = await self._runner(self.command(job_dir, full_prompt, resume_id), env=self.env(),
                                        cwd=self._cfg.repo_root,
                                        on_line=lambda l: state.feed(l, on_event, transcript),
                                        cancel=cancel, on_stderr=transcript.stderr)
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
        self.started: set[str] = set()  # command items whose start was already logged

    def feed(self, line: str, on_event: OnEvent, transcript: Transcript | None = None) -> None:
        event = parse_json_line(line)
        if transcript is not None and line.strip():
            if event is None:
                transcript.event("stdout", {"text": line})
            else:
                transcript.line(line)
        if event is None:
            return
        for summary in codex_summary(event, self.started):
            on_event(summary)
        item = event.get("item") or {}
        if event.get("type") == "item.started" and item.get("id"):
            self.started.add(str(item["id"]))
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            self.session_id = str(event["thread_id"])
        elif kind == "turn.failed":
            self.error = str((event.get("error") or {}).get("message") or "turn failed")
        elif kind == "error":
            self.error = str(event.get("message") or "error")
        elif kind == "turn.completed":
            self.error = None


def codex_summary(event: dict[str, Any], started: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Job-log lines for one `codex exec --json` event. A command is logged when it starts
    (`item.started`) and its exit code and output when it completes; `started` holds the item
    ids already logged as started, so a completion without a start logs both."""
    kind, item = event.get("type"), event.get("item") or {}
    itype = item.get("type")
    if kind == "turn.failed":
        return [f"turn failed: {clip((event.get('error') or {}).get('message') or '')}"]
    if kind == "error":
        return [f"error: {clip(event.get('message') or '')}"]
    if kind not in ("item.started", "item.completed"):
        return []
    if itype == "command_execution":
        call = f"tool Bash: {clip(item.get('command') or '')}"
        if kind == "item.started":
            return [call]
        result = f"result exit={item.get('exit_code')}: {clip(item.get('aggregated_output') or '')}"
        return [result] if str(item.get("id")) in started else [call, result]
    if kind == "item.started":
        return []
    if itype == "file_change":
        changes = [f"{c.get('kind')} {c.get('path')}" for c in item.get("changes") or [] if isinstance(c, dict)]
        return [f"tool file_change: {clip(changes)}", f"result {item.get('status') or 'done'}"]
    if itype == "mcp_tool_call":
        return [f"tool {item.get('server')}.{item.get('tool')}: {clip(item.get('arguments') or '')}",
                f"result {item.get('status') or 'done'}: {clip(item.get('error') or item.get('result') or '')}"]
    if itype == "agent_message":
        return ["say " + clip(item.get("text") or "")]
    if itype == "error":
        return [f"error: {clip(item.get('message') or '')}"]
    return []
