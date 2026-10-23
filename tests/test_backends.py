import asyncio
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ResultMessage

from editor.backends import BackendPool, parse_session_ref, session_ref
from editor.backends.base import AgentOutcome, classify
from editor.backends.claude import MODEL, ClaudeBackend, build_options, make_can_use_tool
from editor.backends.codex import CodexBackend
from editor.backends.grok import GrokBackend
from editor.backends.guard import bash_denied, codex_rules, grok_deny_rules
from editor.backends.proc import ProcResult, run_process
from editor.config import REPO_ROOT
from editor.keychain import MemoryKeychain
from tests.helpers import make_config

TOKEN = "sk-ant-oat01-" + "a" * 90
SKILL_LINE = f"Read {REPO_ROOT}/.claude/skills/infocus-show/SKILL.md first and follow it."


def noop(_line):
    return None


class FakeRunner:
    """Stands in for run_process: records the command and env, replays JSON lines."""

    def __init__(self, lines=(), returncode=0, stderr=""):
        self.lines = list(lines)
        self.returncode = returncode
        self.stderr = stderr
        self.cmd = None
        self.env = None
        self.cwd = None

    async def __call__(self, cmd, *, env, cwd, on_line, cancel, on_stderr=None):
        self.cmd, self.env, self.cwd = list(cmd), dict(env), cwd
        for line in self.lines:
            on_line(json.dumps(line) if isinstance(line, dict) else line)
        if self.stderr and on_stderr:
            on_stderr(self.stderr)
        return ProcResult(self.returncode, self.stderr)


# ---- shared ---------------------------------------------------------------


def test_session_refs_round_trip():
    assert session_ref("codex", "t-1") == "codex:t-1" and session_ref("claude", None) == ""
    assert parse_session_ref("grok:abc-def") == ("grok", "abc-def")
    assert parse_session_ref(None) == (None, None) and parse_session_ref("nocolon") == (None, None)


def test_classify_limits_and_sign_in_problems():
    assert classify("You've hit your usage limit. Try again at 3pm") == (True, False)
    assert classify("Error 429 Too Many Requests") == (True, False)
    assert classify("401 Unauthorized: token has expired") == (False, True)
    assert classify("Not logged in. Please run codex login") == (False, True)
    assert classify("ffmpeg exited 1") == (False, False)


# ---- guard ----------------------------------------------------------------

DENIED_COMMANDS = [
    "sudo ls",
    "echo x | sudo -S tee /etc/hosts",
    "/Library/InFocusEditor/bin/infocus-root update-clt",
    "osascript -e 'do shell script \"id\" with administrator privileges'",
    "launchctl bootout gui/501/com.infocus.editor",
    "security find-generic-password -s infocus-editor -a claude_oauth_token -w",
    "security dump-keychain",
    "pmset -a sleep 1",
    "installer -pkg x.pkg -target /",
    "bash install/user-setup.sh --update",
    "uv run python -m editor.updater",
    "cat ~/InFocusEditor/codex-home/auth.json",
    "echo hi > /etc/sudoers.d/x",
    "cp evil.plist /Library/LaunchDaemons/",
    "mkdir -p '/Library/Application Support/x'",
    "sed -i '' s/a/b/ /private/etc/hosts",
]
ALLOWED_COMMANDS = [
    "ls ~/Library/Logs",
    "uv run --with numpy python .claude/skills/infocus-show/scripts/analyze.py src.mov show",
    "ffmpeg -i '/Library/Fonts/x.ttf' out.png",
    "cp a.mov /Users/editor/InFocusEditor/jobs/x/show/",
    "python3 scripts/progress.py render 'Rendering (security check passed)'",
]


def test_bash_guard_denies_root_keychain_launchd_sign_ins_and_system_writes():
    for cmd in DENIED_COMMANDS:
        assert bash_denied(cmd), cmd
    for cmd in ALLOWED_COMMANDS:
        assert not bash_denied(cmd), cmd


async def test_claude_can_use_tool_applies_the_guards(tmp_path):
    cfg = make_config(tmp_path)
    can_use = make_can_use_tool(cfg.repo_root, cfg.data_dir)
    assert isinstance(await can_use("Bash", {"command": "ffmpeg -version"}, None), PermissionResultAllow)
    assert isinstance(await can_use("Bash", {"command": "sudo -n true"}, None), PermissionResultDeny)
    for name in ("WebFetch", "AskUserQuestion", "mcp__resolve__run_script", "Agent", "Skill"):
        assert isinstance(await can_use(name, {}, None), PermissionResultDeny), name
    for path in ("/etc/sudoers.d/x", str(REPO_ROOT / "editor" / "runner.py"), str(cfg.codex_home / "auth.json"),
                 "~/Library/LaunchAgents/com.infocus.editor.plist"):
        assert isinstance(await can_use("Write", {"file_path": path}, None), PermissionResultDeny), path
    assert isinstance(await can_use("Edit", {"file_path": str(tmp_path / "show" / "edl.json")}, None),
                      PermissionResultAllow)


def test_codex_and_grok_rules_cover_root():
    rules = codex_rules()
    assert 'prefix_rule(pattern=["sudo"], decision="forbidden"' in rules and '"launchctl"' in rules
    deny = grok_deny_rules()
    assert "Bash(sudo *)" in deny and "Bash(launchctl *)" in deny


# ---- claude ---------------------------------------------------------------


def claude_result(session="s1", is_error=False, subtype="success", turns=5, cost=1.25, status=None, errors=None):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=turns,
                         session_id=session, total_cost_usd=cost, api_error_status=status, errors=errors)


class FakeClient:
    instances = []

    def __init__(self, options, messages):
        self.options = options
        self.messages = messages
        self.prompts = []
        FakeClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        for m in self.messages:
            yield m

    async def interrupt(self):
        pass


def claude(tmp_path, *messages, token=TOKEN):
    FakeClient.instances = []
    return ClaudeBackend(make_config(tmp_path), MemoryKeychain({"claude_oauth_token": token} if token else {}),
                         client_factory=lambda options: FakeClient(options, list(messages)))


def test_claude_options_use_the_oauth_token_and_no_mcp(tmp_path):
    cfg = make_config(tmp_path)
    opts = build_options(cfg, tmp_path / "job", TOKEN, resume="s0")
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and "ANTHROPIC_API_KEY" not in opts.env
    assert opts.env["CLAUDE_CONFIG_DIR"] == str(cfg.claude_config_dir)
    assert opts.mcp_servers == {} and opts.model == MODEL == "claude-sonnet-5-5"
    assert opts.tools == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"] and opts.setting_sources == ["project"]
    assert opts.max_turns == 300 and opts.max_budget_usd is None and opts.resume == "s0"
    assert opts.max_buffer_size == 64 * 1024 * 1024 and opts.effort == "high"


async def test_claude_run_starts_with_the_skill_instruction(tmp_path):
    backend = claude(tmp_path, claude_result())
    out = await backend.run(tmp_path, "Edit the show.", None, noop, asyncio.Event())
    assert out == AgentOutcome(ok=True, backend="claude", session_id="s1", cost_usd=1.25)
    assert FakeClient.instances[0].prompts[0].startswith(SKILL_LINE)


async def test_claude_usage_limit_and_sign_in_failure(tmp_path):
    out = await claude(tmp_path, claude_result(is_error=True, subtype="success", status=429)).run(
        tmp_path, "x", None, noop, asyncio.Event())
    assert out.limit_hit and not out.auth_failed
    out = await claude(tmp_path, claude_result(is_error=True, subtype="success", status=401)).run(
        tmp_path, "x", None, noop, asyncio.Event())
    assert out.auth_failed and not out.limit_hit
    out = await claude(tmp_path, claude_result(is_error=True, subtype="error_max_turns")).run(
        tmp_path, "x", None, noop, asyncio.Event())
    assert not out.ok and not out.limit_hit and "turn limit" in out.error


async def test_claude_without_a_token_is_not_connected(tmp_path):
    backend = claude(tmp_path, token=None)
    assert backend.status().connected is False
    out = await backend.run(tmp_path, "x", None, noop, asyncio.Event())
    assert out.auth_failed
    assert claude(tmp_path, token="sk-ant-api03-not-a-subscription").status().connected is False


async def test_claude_failed_resume_has_no_session(tmp_path):
    out = await claude(tmp_path, claude_result(session="dead", is_error=True, subtype="error_during_execution",
                                               turns=0)).run(tmp_path, "x", "dead", noop, asyncio.Event())
    assert not out.ok and out.session_id is None


# ---- codex ----------------------------------------------------------------


def connected_codex(tmp_path, runner):
    cfg = make_config(tmp_path)
    cfg.codex_home.mkdir(parents=True)
    (cfg.codex_home / "auth.json").write_text('{"tokens": {"access_token": "x"}}')
    return CodexBackend(cfg, runner=runner), cfg


async def test_codex_command_env_and_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    runner = FakeRunner([{"type": "thread.started", "thread_id": "0199-thread"}, {"type": "turn.started"},
                         {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
                         {"type": "turn.completed", "usage": {"input_tokens": 1}}])
    backend, cfg = connected_codex(tmp_path, runner)
    job = tmp_path / "job"
    out = await backend.run(job, "Edit the show.", None, noop, asyncio.Event())
    assert out == AgentOutcome(ok=True, backend="codex", session_id="0199-thread")
    cmd = runner.cmd
    assert cmd[:2] == ["codex", "exec"] and "--json" in cmd and "--skip-git-repo-check" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-6.1-sol"
    assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="low"'
    assert cmd[cmd.index("-C") + 1] == str(cfg.repo_root)
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-dir"] == [str(job), str(cfg.package_dir),
                                                                           str(cfg.data_dir)]
    assert cmd[-1].startswith(SKILL_LINE)
    assert runner.env["CODEX_HOME"] == str(cfg.codex_home) and "OPENAI_API_KEY" not in runner.env
    assert "mcp_servers" not in (cfg.codex_home / "config.toml").read_text()
    assert "forbidden" in (cfg.codex_home / "rules" / "infocus.rules").read_text()
    assert stat.S_IMODE(cfg.codex_home.stat().st_mode) == 0o700


async def test_codex_resume_uses_the_resume_subcommand(tmp_path):
    runner = FakeRunner([{"type": "turn.completed"}])
    backend, cfg = connected_codex(tmp_path, runner)
    out = await backend.run(tmp_path, "Fix it.", "0199-thread", noop, asyncio.Event())
    assert runner.cmd[:3] == ["codex", "exec", "resume"] and runner.cmd[-2] == "0199-thread"
    assert "-C" not in runner.cmd and runner.cwd == cfg.repo_root
    assert out.session_id == "0199-thread"


async def test_codex_usage_limit_is_reported(tmp_path):
    runner = FakeRunner([{"type": "thread.started", "thread_id": "t"},
                         {"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}],
                        returncode=1)
    backend, _ = connected_codex(tmp_path, runner)
    out = await backend.run(tmp_path, "x", None, noop, asyncio.Event())
    assert out.limit_hit and out.session_id == "t"


async def test_codex_not_signed_in(tmp_path):
    backend = CodexBackend(make_config(tmp_path), runner=FakeRunner())
    assert backend.status().connected is False
    assert (await backend.run(tmp_path, "x", None, noop, asyncio.Event())).auth_failed


def test_codex_verify_uses_login_status(tmp_path):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return subprocess.CompletedProcess(cmd, 0, "Logged in using ChatGPT", "")

    backend = CodexBackend(make_config(tmp_path), run=run)
    assert backend.verify() is None
    assert seen["cmd"] == ["codex", "login", "status"] and seen["env"]["CODEX_HOME"].endswith("codex-home")


# ---- grok -----------------------------------------------------------------


def connected_grok(tmp_path, runner):
    cfg = make_config(tmp_path)
    cfg.grok_home.mkdir(parents=True)
    (cfg.grok_home / "auth.json").write_text('{"access_token": "x"}')
    return GrokBackend(cfg, runner=runner, new_id=lambda: "11111111-2222-3333-4444-555555555555"), cfg


async def test_grok_command_env_and_fixed_session_id(tmp_path):
    runner = FakeRunner([{"type": "tool_call", "toolName": "bash"},
                         {"type": "end", "stopReason": "end_turn", "sessionId": "11111111-2222-3333-4444-555555555555"}])
    backend, cfg = connected_grok(tmp_path, runner)
    out = await backend.run(tmp_path / "job", "Edit the show.", None, noop, asyncio.Event())
    assert out.ok and out.session_id == "11111111-2222-3333-4444-555555555555"
    cmd = runner.cmd
    assert cmd[0] == "grok" and cmd[cmd.index("-p") + 1].startswith(SKILL_LINE)
    assert cmd[cmd.index("--output-format") + 1] == "streaming-json"
    assert cmd[cmd.index("--cwd") + 1] == str(cfg.repo_root)
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert cmd[cmd.index("--max-turns") + 1] == "300"
    assert cmd[cmd.index("--reasoning-effort") + 1] == "medium"
    assert cmd[cmd.index("-s") + 1] == "11111111-2222-3333-4444-555555555555" and "--resume" not in cmd
    assert "Bash(sudo *)" in [cmd[i + 1] for i, a in enumerate(cmd) if a == "--deny"]
    assert runner.env["GROK_HOME"] == str(cfg.grok_home)


async def test_grok_resume_and_auth_failure(tmp_path):
    runner = FakeRunner([{"type": "error", "message": "Authentication failed: please run grok login"}],
                        returncode=1)
    backend, _ = connected_grok(tmp_path, runner)
    out = await backend.run(tmp_path, "Fix it.", "abc", noop, asyncio.Event())
    assert runner.cmd[runner.cmd.index("--resume") + 1] == "abc" and "-s" not in runner.cmd
    assert out.auth_failed and out.session_id == "abc"


# ---- process groups -------------------------------------------------------


async def test_cancel_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "pids"
    script = f"sleep 60 & echo $! > {pidfile}; echo started; wait"
    cancel = asyncio.Event()
    lines = []
    task = asyncio.create_task(run_process(["/bin/sh", "-c", script], env=dict(os.environ), cwd=tmp_path,
                                           on_line=lines.append, cancel=cancel, grace_s=1))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.02)
    child = int(pidfile.read_text())
    cancel.set()
    result = await asyncio.wait_for(task, 20)
    assert result.cancelled and lines == ["started"]
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


# ---- pool -----------------------------------------------------------------


def test_pool_order_and_statuses(tmp_path):
    class B:
        def __init__(self, name, connected):
            self.name, self.label, self._c = name, name.title(), connected

        def status(self):
            from editor.backends import BackendStatus
            return BackendStatus(self.name, self._c, "ok")

    pool = BackendPool([B("grok", True), B("claude", True), B("codex", False)], ["claude", "codex", "grok"])
    assert [b.name for b in pool.connected()] == ["claude", "grok"]
    pool.note(AgentOutcome(ok=False, backend="claude", session_id=None, auth_failed=True))
    assert [b.name for b in pool.connected()] == ["grok"]
    assert [s.connected for s in pool.statuses()] == [False, False, True]
    pool.reset("claude")
    assert [b.name for b in pool.connected()] == ["claude", "grok"]


async def test_leftover_background_child_is_stopped_after_a_normal_exit(tmp_path):
    pidfile = tmp_path / "pids"
    script = f"sleep 60 & echo $! > {pidfile}; echo done"
    result = await asyncio.wait_for(run_process(["/bin/sh", "-c", script], env=dict(os.environ), cwd=tmp_path,
                                                on_line=noop, cancel=asyncio.Event(), grace_s=1), 30)
    assert result.returncode == 0 and not result.cancelled
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


# ---- transcripts and tool-call summaries ------------------------------------------


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_claude_writes_a_transcript_per_session_and_summarizes_tools(tmp_path):
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

    from editor.backends.claude import claude_summary

    messages = [
        AssistantMessage(content=[TextBlock("Looking at the recording."),
                                  ToolUseBlock("tu1", "Bash", {"command": "ffprobe src/01.mov", "timeout": 60})],
                         model="claude-opus-5-5"),
        UserMessage(content=[ToolResultBlock("tu1", "duration=480.0\nok", is_error=False)]),
        AssistantMessage(content=[ToolUseBlock("tu2", "Write", {"file_path": "show/edl.json", "content": "{}"})],
                         model="claude-opus-5-5"),
        UserMessage(content=[ToolResultBlock("tu2", [{"type": "text", "text": "denied"}], is_error=True)]),
        claude_result(),
    ]
    events = []
    job = tmp_path / "job"
    backend = claude(tmp_path, *messages)
    out = await backend.run(job, "Edit the show.", None, events.append, asyncio.Event())
    assert out.turns == 5
    await claude(tmp_path, claude_result(session="s2")).run(job, "Again.", "s1", noop, asyncio.Event())
    first = read_jsonl(job / "logs" / "agent-01-claude.jsonl")
    assert [e["type"] for e in first] == ["AssistantMessage", "UserMessage", "AssistantMessage", "UserMessage",
                                          "ResultMessage"]
    assert first[0]["content"][1] == {"id": "tu1", "name": "Bash",
                                      "input": {"command": "ffprobe src/01.mov", "timeout": 60}}
    assert first[-1]["session_id"] == "s1" and first[-1]["num_turns"] == 5
    assert read_jsonl(job / "logs" / "agent-02-claude.jsonl")[0]["session_id"] == "s2"
    assert events == ["say Looking at the recording.", "tool Bash: ffprobe src/01.mov", "result Bash ok: duration=480.0 ok",
                      'tool Write: {"file_path": "show/edl.json", "content": "{}"}', "result Write error: denied",
                      "end success turns=5"]
    assert claude_summary(UserMessage(content="plain prompt text"), {}) == []


def test_claude_transcript_falls_back_to_repr():
    from editor.backends.claude import serialize_message

    class Odd:
        def __repr__(self):
            return "<odd>"

    assert serialize_message(Odd()) == {"repr": "<odd>"}


async def test_codex_writes_its_stdout_lines_and_stderr(tmp_path):
    lines = [{"type": "thread.started", "thread_id": "t1"},
             {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "ls show",
                                                "status": "in_progress"}},
             {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "ls show",
                                                  "aggregated_output": "edl.json\n", "exit_code": 0}},
             "not json at all",
             {"type": "turn.completed", "usage": {"input_tokens": 1}}]
    runner = FakeRunner(lines, stderr="warning: refresh_token=abc123 expired\n")
    backend, _ = connected_codex(tmp_path, runner)
    events = []
    job = tmp_path / "job"
    await backend.run(job, "Edit the show.", None, events.append, asyncio.Event())
    transcript = read_jsonl(job / "logs" / "agent-01-codex.jsonl")
    assert transcript[:3] == lines[:3] and transcript[3] == {"type": "stdout", "text": "not json at all"}
    assert (job / "logs" / "agent-01-codex.stderr.log").read_text() == "warning: refresh_token=[hidden] expired\n"
    assert events == ["tool Bash: ls show", "result exit=0: edl.json"]


def test_codex_summary_lines():
    from editor.backends.codex import codex_summary

    done = {"type": "item.completed", "item": {"id": "i9", "type": "command_execution", "command": "false",
                                               "aggregated_output": "boom", "exit_code": 1}}
    assert codex_summary(done) == ["tool Bash: false", "result exit=1: boom"]  # no start seen
    assert codex_summary(done, {"i9"}) == ["result exit=1: boom"]
    assert codex_summary({"type": "item.completed", "item": {"type": "file_change", "status": "completed",
                                                             "changes": [{"path": "show/edl.json", "kind": "update"}]}}
                         ) == ['tool file_change: ["update show/edl.json"]', "result completed"]
    assert codex_summary({"type": "turn.failed", "error": {"message": "usage limit"}}) == ["turn failed: usage limit"]
    long = codex_summary({"type": "item.started", "item": {"id": "i", "type": "command_execution",
                                                           "command": "x" * 1000}})[0]
    assert len(long) == len("tool Bash: ") + 300
    assert codex_summary({"type": "turn.started"}) == []


async def test_grok_writes_its_stdout_lines_and_summarizes_tools(tmp_path):
    lines = [{"type": "tool_call", "toolCallId": "c1", "toolName": "run_terminal_cmd", "status": "in_progress",
              "rawInput": {"command": "uv run python scripts/progress.py render Rendering"}},
             {"type": "tool_call_update", "toolCallId": "c1", "status": "in_progress", "rawOutput": {}},
             {"type": "tool_call_update", "toolCallId": "c1", "status": "completed",
              "rawOutput": {"exitCode": 0, "output": "ok"}},
             {"type": "end", "stopReason": "end_turn", "sessionId": "11111111-2222-3333-4444-555555555555"}]
    runner = FakeRunner(lines, stderr="note\n")
    backend, _ = connected_grok(tmp_path, runner)
    events = []
    job = tmp_path / "job"
    await backend.run(job, "Edit the show.", None, events.append, asyncio.Event())
    assert read_jsonl(job / "logs" / "agent-01-grok.jsonl") == lines
    assert (job / "logs" / "agent-01-grok.stderr.log").read_text() == "note\n"
    assert events == ["tool run_terminal_cmd: uv run python scripts/progress.py render Rendering",
                      "result completed exit=0: ok"]


def test_grok_summary_lines():
    from editor.backends.grok import grok_summary

    assert grok_summary({"type": "tool_call", "title": "Read", "rawInput": {"path": "a.json"}}) == [
        'tool Read: {"path": "a.json"}']
    failed = {"type": "tool_call_update", "status": "failed",
              "content": [{"type": "content", "content": {"type": "text", "text": "No such file"}}]}
    assert grok_summary(failed) == ["result failed: No such file"]
    assert grok_summary({"type": "error", "message": "Authentication failed"}) == ["error: Authentication failed"]
    assert grok_summary({"type": "usage"}) == []
