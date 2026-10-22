"""Remote shell for the super admin: a command signed with the admin key runs on a dedicated editing Mac.

The Portal only carries it, as a command whose kind is `shell:<payload>.<signature>` (base64url). Nothing in the
Portal can make one: it's written straight to the database by `scripts/editor-shell.mjs` in the Portal repo, signed
with the private key that matches `admin_shell.pub`. The editor runs it only if:
- this is a dedicated machine (a personal computer refuses every one),
- the Ed25519 signature checks out against `admin_shell.pub`,
- the payload names this command's id and this machine (sha256 of its device token, as the Portal stores it),
- and it hasn't expired (10 minutes after signing).
It runs as the editor's own macOS account (no root), with a time limit, and the output goes back as the command's ack.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

SHELL_PREFIX = "shell:"
PUBLIC_KEY_FILE = Path(__file__).with_name("admin_shell.pub")
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 1800
MAX_VALIDITY_S = 3600  # an expiry further out than this is refused too
OUTPUT_TAIL = 3900


class ShellRefused(Exception):
    pass


@dataclass(frozen=True)
class ShellRequest:
    cmd: str
    timeout_s: int


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify(kind: str, command_id: str, token_hash: str, now: float, public_key: str) -> ShellRequest:
    """The command to run, or ShellRefused saying why not."""
    body, dot, signature = kind.removeprefix(SHELL_PREFIX).partition(".")
    if not kind.startswith(SHELL_PREFIX) or not dot or not public_key.strip():
        raise ShellRefused("not a signed shell command")
    try:
        VerifyKey(base64.b64decode(public_key.strip())).verify(body.encode(), _unb64(signature))
        payload = json.loads(_unb64(body))
    except (BadSignatureError, ValueError, TypeError) as e:
        raise ShellRefused(f"bad signature or payload ({e.__class__.__name__})") from e
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ShellRefused("unknown payload version")
    if payload.get("id") != command_id:
        raise ShellRefused("signed for another command")
    if payload.get("device") != token_hash:
        raise ShellRefused("signed for another machine")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or not now < exp <= now + MAX_VALIDITY_S:
        raise ShellRefused("expired")
    cmd = payload.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ShellRefused("no command")
    timeout = payload.get("timeout", DEFAULT_TIMEOUT_S)
    timeout = int(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_TIMEOUT_S
    return ShellRequest(cmd, min(timeout, MAX_TIMEOUT_S))


async def run(request: ShellRequest, cwd: Path | None = None) -> tuple[bool, str]:
    """zsh -lc <cmd> as this account; (exit code 0, output tail with the exit status)."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/zsh", "-lc", request.cmd, cwd=str(cwd or Path.home()), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    seen = bytearray()  # kept as it arrives, so a killed command still reports what it printed

    async def read_all() -> None:
        assert proc.stdout is not None
        while data := await proc.stdout.read(65536):
            seen.extend(data)
            del seen[:-OUTPUT_TAIL * 4]

    try:
        await asyncio.wait_for(asyncio.gather(read_all(), proc.wait()), timeout=request.timeout_s)
    except TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        text = seen.decode(errors="replace")[-OUTPUT_TAIL:]
        return False, f"{text}\n[timed out after {request.timeout_s} s; killed]"
    text = seen.decode(errors="replace")[-OUTPUT_TAIL:]
    return proc.returncode == 0, f"{text}\n[exit {proc.returncode}]"


class RemoteShell:
    """What the worker calls for a `shell:` command: refuse, or verify and run."""

    def __init__(self, machine_kind: str, device_token: Callable[[], str | None],
                 public_key: Callable[[], str] = lambda: PUBLIC_KEY_FILE.read_text() if PUBLIC_KEY_FILE.exists() else "",
                 clock: Callable[[], float] = time.time):
        self._kind = machine_kind
        self._token = device_token
        self._public_key = public_key
        self._clock = clock

    async def __call__(self, command_id: str, kind: str) -> tuple[bool, str]:
        if self._kind != "dedicated":
            return False, "Refused: remote commands only run on a dedicated editing Mac."
        token = self._token()
        if not token:
            return False, "Refused: this Mac isn't linked."
        try:
            request = verify(kind, command_id, hashlib.sha256(token.encode()).hexdigest(), self._clock(),
                             self._public_key())
        except ShellRefused as e:
            return False, f"Refused: {e}."
        return await run(request)
