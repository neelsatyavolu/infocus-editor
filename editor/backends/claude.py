"""Claude through the Agent SDK, signed in with a `claude setup-token` OAuth
token (Claude subscription; no API key). Shell and file tools only."""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from editor.backends.base import AgentOutcome, BackendStatus, OnEvent, classify, failure
from editor.backends.guard import bash_denied, write_denied
from editor.config import Config
from editor.joblog import Transcript, clip
from editor.prompts import UNATTENDED, instruction

# The producer's choice for quality per cost: Sonnet 5.5 at high effort. Most of an edit's tokens are cache reads,
# which cost the same on Sonnet and Opus; Sonnet halves the rest.
MODEL = "claude-sonnet-5-5"
EFFORT = "high"
TOKEN_ITEM = "claude_oauth_token"
TOKEN_RE = re.compile(r"^sk-ant-oat01-[A-Za-z0-9_-]{40,}$")
TOOLS = ("Read", "Write", "Edit", "Bash", "Glob", "Grep")
_WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")
# The SDK's default 1 MB cap per CLI message is smaller than a Read of one full-size contact sheet, and going over it
# kills the session ("JSON message exceeded maximum buffer size").
MAX_BUFFER_BYTES = 64 * 1024 * 1024
BUNDLED_CLI = Path(__import__("claude_agent_sdk").__file__).parent / "_bundled" / "claude"


def make_can_use_tool(repo_root: Path, data_dir: Path):
    async def can_use_tool(name: str, tool_input: dict[str, Any], context: Any):
        """Deny by default: shell and file tools only, never root or the editor's own files."""
        if name not in TOOLS:
            return PermissionResultDeny(message=f"{name} isn't available to the unattended editor.")
        if name == "Bash" and bash_denied(str(tool_input.get("command") or "")):
            return PermissionResultDeny(
                message="That command touches admin rights, the Keychain, launchd, system folders or the "
                        "editor's installer, updater or sign-in files, which the editing agent may not use.")
        if name in _WRITE_TOOLS and write_denied(str(tool_input.get("file_path") or ""), repo_root, data_dir):
            return PermissionResultDeny(message="The editing agent may not write there.")
        return PermissionResultAllow()

    return can_use_tool


def build_options(cfg: Config, job_dir: Path, token: str, resume: str | None = None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        effort=EFFORT,
        cwd=str(cfg.repo_root),
        setting_sources=["project"],
        tools=list(TOOLS),
        allowed_tools=[],
        can_use_tool=make_can_use_tool(cfg.repo_root, cfg.data_dir),
        permission_mode="default",
        add_dirs=[str(job_dir), str(cfg.package_dir), str(cfg.data_dir)],
        mcp_servers={},
        system_prompt={"type": "preset", "preset": "claude_code", "append": UNATTENDED},
        env={
            "CLAUDE_CODE_OAUTH_TOKEN": token,
            # Keep the person's own ~/.claude (logins, memories, plugins) out of the session.
            "CLAUDE_CONFIG_DIR": str(cfg.claude_config_dir),
        },
        max_turns=cfg.max_turns,
        max_budget_usd=cfg.max_budget_usd,
        resume=resume,
        max_buffer_size=MAX_BUFFER_BYTES,
    )


class ClaudeBackend:
    name = "claude"
    label = "Claude"

    def __init__(self, cfg: Config, keychain, *, client_factory: Callable[..., Any] = ClaudeSDKClient,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._cfg = cfg
        self._keychain = keychain
        self._factory = client_factory
        self._run = run

    def _token(self) -> str | None:
        token = self._keychain.get(TOKEN_ITEM)
        return token if token and TOKEN_RE.match(token) else None

    def status(self) -> BackendStatus:
        if self._token():
            return BackendStatus(self.name, True, "Signed in with a Claude subscription token")
        return BackendStatus(self.name, False, "Not connected")

    def verify(self) -> str | None:
        token = self._token()
        if token is None:
            return "That isn't a Claude subscription token (it should start with sk-ant-oat01-)."
        env = {"CLAUDE_CODE_OAUTH_TOKEN": token, "CLAUDE_CONFIG_DIR": str(self._cfg.claude_config_dir),
               "PATH": "/usr/bin:/bin", "HOME": str(Path.home())}
        try:
            proc = self._run([str(BUNDLED_CLI), "-p", "Reply with the single word OK.", "--max-turns", "1",
                              "--output-format", "json"], capture_output=True, text=True, check=False,
                             timeout=120, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Couldn't run the Claude CLI to check the token: {e.__class__.__name__}"
        if proc.returncode != 0:
            limit, _ = classify(proc.stdout + proc.stderr)
            return None if limit else "Claude rejected the token; connect Claude again."
        return None

    async def run(self, job_dir: Path, prompt: str, resume_id: str | None, on_event: OnEvent,
                  cancel: asyncio.Event) -> AgentOutcome:
        token = self._token()
        if token is None:
            return AgentOutcome(ok=False, backend=self.name, session_id=None, auth_failed=True,
                                error="Claude isn't connected")
        options = build_options(self._cfg, job_dir, token, resume_id)
        with Transcript(job_dir, self.name) as transcript:
            try:
                return await self._session(options, instruction(self._cfg.repo_root, prompt, with_rules=False),
                                           on_event, cancel, transcript)
            except Exception as e:  # noqa: BLE001 - the CLI process can fail in many ways
                return failure(self.name, None, f"Claude session failed: {e.__class__.__name__}: {e}",
                               cancelled=cancel.is_set())

    async def _session(self, options: ClaudeAgentOptions, prompt: str, on_event: OnEvent,
                       cancel: asyncio.Event, transcript: Transcript) -> AgentOutcome:
        result: ResultMessage | None = None
        tool_names: dict[str, str] = {}
        async with self._factory(options=options) as client:
            watcher = asyncio.create_task(_interrupt_on(cancel, client))
            try:
                await client.query(prompt)
                async for message in client.receive_response():
                    transcript.event(type(message).__name__, serialize_message(message))
                    for line in claude_summary(message, tool_names):
                        on_event(line)
                    if isinstance(message, ResultMessage):
                        result = message
            finally:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
        if result is None:
            return failure(self.name, None, "the session ended without a result", cancelled=cancel.is_set())
        cost = float(result.total_cost_usd or 0.0)
        if not result.is_error and result.subtype == "success":
            return AgentOutcome(ok=True, backend=self.name, session_id=result.session_id, cost_usd=cost,
                                turns=result.num_turns)
        resumed_nothing = result.num_turns == 0 and options.resume
        session = None if resumed_nothing else result.session_id
        status = result.api_error_status
        text = _describe(result)
        outcome = replace(failure(self.name, session, text, cancelled=cancel.is_set(), cost_usd=cost),
                          turns=result.num_turns)
        if status == 429 and not cancel.is_set():
            return replace(outcome, limit_hit=True, auth_failed=False)
        if status in (401, 403) and not cancel.is_set():
            return replace(outcome, auth_failed=True, limit_hit=False)
        return outcome


async def _interrupt_on(cancel: asyncio.Event, client: Any) -> None:
    await cancel.wait()
    with contextlib.suppress(Exception):
        await client.interrupt()


def serialize_message(message: Any) -> dict[str, Any]:
    """An SDK message as plain data for the transcript (non-JSON values become repr() when written)."""
    if dataclasses.is_dataclass(message) and not isinstance(message, type):
        try:
            return dataclasses.asdict(message)
        except Exception:  # noqa: BLE001 - a value that can't be copied
            pass
    return {"repr": repr(message)}


def claude_summary(message: Any, tool_names: dict[str, str]) -> list[str]:
    """Job-log lines for one SDK message: tool calls, tool results, what the agent says.
    `tool_names` maps tool_use ids to names, so a result line can say which tool it answers."""
    lines = []
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                tool_names[block.id] = block.name
                args = block.input.get("command") if block.name == "Bash" and "command" in block.input else block.input
                lines.append(f"tool {block.name}: {clip(args)}")
            elif isinstance(block, TextBlock) and block.text.strip():
                lines.append("say " + clip(block.text))
    elif isinstance(message, UserMessage) and isinstance(message.content, list):
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                status = "error" if block.is_error else "ok"
                lines.append(f"result {tool_names.get(block.tool_use_id, '?')} {status}: {clip(_text(block.content))}")
    elif isinstance(message, ResultMessage):
        lines.append(f"end {message.subtype} turns={message.num_turns}")
    return lines


def _text(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text") or "") if isinstance(c, dict) else str(c) for c in content)
    return "" if content is None else str(content)


def _describe(result: ResultMessage) -> str:
    reasons = {
        "error_max_turns": "the session hit its turn limit",
        "error_max_budget_usd": "the session hit its spend cap",
    }
    if result.subtype in reasons:
        return reasons[result.subtype]
    detail = "; ".join(result.errors or []) or result.result or result.terminal_reason or result.subtype
    return f"the session ended with an error: {detail}"[:500]
