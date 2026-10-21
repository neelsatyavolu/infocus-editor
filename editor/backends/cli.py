"""Shared pieces for backends that drive a command-line agent (Codex, Grok)."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

# API keys in the worker's environment must never take over from the subscription sign-in.
_STRIPPED = ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "GROK_API_KEY",
             "CLAUDE_CODE_OAUTH_TOKEN")
PATH_DIRS = ("/opt/homebrew/bin", str(Path.home() / ".local" / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def agent_env(extra: Mapping[str, str], base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in _STRIPPED}
    path = env.get("PATH", "")
    env["PATH"] = ":".join(dict.fromkeys([*PATH_DIRS, *[p for p in path.split(":") if p]]))
    env.update(extra)
    return env


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IRWXU)
    return path


def read_auth(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def write_private(path: Path, text: str) -> None:
    """Write a credential file readable only by this user (0600), atomically."""
    private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def parse_json_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
