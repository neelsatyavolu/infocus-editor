"""`infocus-editor connect <claude|codex|grok> --code XXXX-XXXX --portal URL`

Runs on a producer's own computer (e.g. `uvx --from git+<repo> infocus-editor connect …`),
with no config or Keychain. It signs in to the AI service locally, seals the
credential to the editing Mac's public key and hands the ciphertext to the Portal,
which can't read it. Secrets are never printed or logged.

Codex and Grok get a fresh sign-in in a throwaway home instead of copying the
person's own ~/.codex or ~/.grok: refresh tokens rotate, so sharing one would
sign the other machine out.
"""
from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import httpx

from editor.config import BACKEND_NAMES
from editor.pairing import seal

CODE_RE = re.compile(r"^[A-Za-z0-9]{4}-[A-Za-z0-9]{4}$")
TOKEN_PREFIX = "sk-ant-oat01-"
TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{40,}")
MASK = "…(captured, not shown)"

Write = Callable[[str], Any]


class ConnectError(Exception):
    """A plain message for the person running connect."""


# ---- the Claude token, captured from `claude setup-token` through a pty ------


class TokenMasker:
    """Passes terminal output through while hiding (and keeping) any subscription token."""

    def __init__(self) -> None:
        self._buf = ""
        self._current = ""
        self._in_token = False
        self.tokens: list[str] = []

    def feed(self, text: str) -> str:
        out: list[str] = []
        s, self._buf = self._buf + text, ""
        i = 0
        while i < len(s):
            if self._in_token:
                if re.match(r"[A-Za-z0-9_-]", s[i]):
                    self._current += s[i]
                    i += 1
                    continue
                self._finish(out)
                continue
            j = s.find(TOKEN_PREFIX, i)
            if j == -1:
                rest = s[i:]
                keep = next((n for n in range(min(len(rest), len(TOKEN_PREFIX) - 1), 0, -1)
                             if TOKEN_PREFIX.startswith(rest[-n:])), 0)
                out.append(rest[:len(rest) - keep])
                self._buf = rest[len(rest) - keep:]
                break
            out.append(s[i:j] + TOKEN_PREFIX)
            self._in_token, self._current = True, TOKEN_PREFIX
            i = j + len(TOKEN_PREFIX)
        return "".join(out)

    def flush(self) -> str:
        out: list[str] = []
        if self._in_token:
            self._finish(out)
        tail, self._buf = self._buf, ""
        return "".join(out) + tail

    def _finish(self, out: list[str]) -> None:
        self._in_token = False
        self.tokens.append(self._current)
        self._current = ""
        out.append(MASK)

    @property
    def token(self) -> str | None:
        full = [t for t in self.tokens if TOKEN_RE.fullmatch(t)]
        return full[-1] if full else None


def capture_pty(cmd: list[str], env: Mapping[str, str] | None = None, write: Write | None = None) -> tuple[int, str | None]:
    """Run an interactive CLI on a wide pty, mirror it to the terminal with the token masked,
    and return (exit code, token)."""
    import fcntl
    import pty
    import struct
    import termios
    import tty

    write = write or (lambda s: (sys.stdout.write(s), sys.stdout.flush()))
    master, slave = pty.openpty()
    # Wide, so the token is never wrapped across lines.
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 400, 0, 0))
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, env=dict(env or os.environ),
                            close_fds=True, start_new_session=True)
    os.close(slave)
    masker = TokenMasker()
    stdin_fd = sys.stdin.fileno() if sys.stdin and sys.stdin.isatty() else None
    saved = termios.tcgetattr(stdin_fd) if stdin_fd is not None else None
    try:
        if stdin_fd is not None:
            tty.setraw(stdin_fd)
        while True:
            fds = [master] + ([stdin_fd] if stdin_fd is not None else [])
            ready, _, _ = select.select(fds, [], [], 0.2)
            if master in ready:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                write(masker.feed(data.decode("utf-8", "replace")))
            if stdin_fd is not None and stdin_fd in ready:
                os.write(master, os.read(stdin_fd, 1024))
            if proc.poll() is not None and master not in ready:
                break
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        os.close(master)
    write(masker.flush())
    return proc.wait(), masker.token


def claude_cli() -> str:
    found = shutil.which("claude")
    if found:
        return found
    import claude_agent_sdk

    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.exists():
        return str(bundled)
    raise ConnectError("The Claude CLI isn't available; install Claude Code and try again.")


def signin_claude(device_auth: bool, capture=capture_pty) -> dict[str, str]:
    print("Signing in to Claude. Approve in the browser; the token is captured, not shown.")
    code, token = capture([claude_cli(), "setup-token"])
    if not token:
        raise ConnectError("Didn't get a Claude token from `claude setup-token`"
                           + (f" (it exited {code})." if code else "."))
    return {"backend": "claude", "kind": "oauth_token", "value": token}


# ---- Codex and Grok: a fresh sign-in in a throwaway home -------------------

LOGIN = {
    "codex": ("CODEX_HOME", lambda device: ["codex", "login"] + (["--device-auth"] if device else [])),
    "grok": ("GROK_HOME", lambda device: ["grok", "login"] + (["--device-auth"] if device else ["--oauth"])),
}


def signin_home(backend: str, device_auth: bool, run=subprocess.run) -> dict[str, str]:
    env_name, command = LOGIN[backend]
    cmd = command(device_auth)
    if shutil.which(cmd[0]) is None and run is subprocess.run:
        raise ConnectError(f"The {cmd[0]} CLI isn't installed on this computer.")
    with tempfile.TemporaryDirectory(prefix=f"infocus-{backend}-") as tmp:
        home = Path(tmp)
        try:
            print(f"Signing in to {backend.title()} in a fresh, temporary profile.")
            proc = run(cmd, env={**os.environ, env_name: str(home)}, check=False)
            auth = home / "auth.json"
            if proc.returncode != 0 or not auth.is_file():
                raise ConnectError(f"{backend.title()} sign-in didn't finish.")
            value = auth.read_text()
        finally:
            _wipe(home)
    return {"backend": backend, "kind": "auth_json", "value": value}


def _wipe(home: Path) -> None:
    for path in home.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                path.write_bytes(b"\0" * path.stat().st_size)
            except OSError:
                pass


# ---- the whole flow ----------------------------------------------------------


def check_portal(url: str) -> str:
    parsed = urlparse(url)
    local = parsed.hostname in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ConnectError("The Portal address must start with https://")
    return url.rstrip("/")


def connect(backend: str, code: str, portal: str, *, device_auth: bool = False,
            transport: httpx.BaseTransport | None = None,
            signin: Callable[[str, bool], dict[str, str]] | None = None) -> None:
    if backend not in BACKEND_NAMES:
        raise ConnectError(f"Pick one of: {', '.join(BACKEND_NAMES)}.")
    if not CODE_RE.match(code):
        raise ConnectError("The code looks like XXXX-XXXX; copy it from the Portal.")
    base = f"{check_portal(portal)}/api/service/show-editor/pair/{code.upper()}"
    with httpx.Client(transport=transport, timeout=30) as http:
        res = http.get(base)
        if res.status_code == 404:
            raise ConnectError("That code is unknown, expired or already used. Make a new one in the Portal.")
        res.raise_for_status()
        info = (res.json() or {}).get("data") or {}
        if info.get("backend") != backend:
            raise ConnectError(f"That code is for {info.get('backend')}, not {backend}.")
        payload = (signin or _default_signin)(backend, device_auth)
        ciphertext = seal(str(info["publicKey"]), payload)
        payload.clear()
        res = http.post(base, json={"ciphertext": ciphertext})
        if res.status_code == 404:
            raise ConnectError("The code expired while you were signing in. Make a new one and try again.")
        res.raise_for_status()
    print(f"Connected {backend.title()}. The editing Mac picks it up within a minute; "
          "check Automatic editing in the Portal.")


def _default_signin(backend: str, device_auth: bool) -> dict[str, str]:
    if backend == "claude":
        return signin_claude(device_auth)
    return signin_home(backend, device_auth)
