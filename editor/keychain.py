"""macOS Keychain items under the service `infocus-editor`. Secret values go
to `security` on stdin, never on the command line, and are never logged."""
from __future__ import annotations

import re
import subprocess
from typing import Callable

SERVICE = "infocus-editor"
NAME_RE = re.compile(r"^[a-z_]{1,40}$")
# Tokens and base64 keys only: anything else would need quoting in `security -i`.
VALUE_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,8192}$")

Run = Callable[..., subprocess.CompletedProcess]


class KeychainError(Exception):
    pass


class Keychain:
    def __init__(self, run: Run = subprocess.run, service: str = SERVICE):
        self._run = run
        self._service = service

    def get(self, name: str) -> str | None:
        _check_name(name)
        proc = self._run(["security", "find-generic-password", "-s", self._service, "-a", name, "-w"],
                         capture_output=True, text=True, check=False)
        value = (proc.stdout or "").strip()
        return value if proc.returncode == 0 and value else None

    def set(self, name: str, value: str) -> None:
        _check_name(name)
        if not VALUE_RE.match(value):
            raise KeychainError(f"refusing to store {name}: unexpected characters")
        command = f"add-generic-password -U -s {self._service} -a {name} -w {value}\n"
        proc = self._run(["security", "-i"], input=command, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or "error" in (proc.stderr or "").lower():
            raise KeychainError(f"couldn't save {name} in the Keychain")

    def delete(self, name: str) -> None:
        _check_name(name)
        self._run(["security", "delete-generic-password", "-s", self._service, "-a", name],
                  capture_output=True, check=False)


class MemoryKeychain:
    """For tests and for `connect`, which runs on someone else's computer."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def _check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise KeychainError(f"bad Keychain item name {name!r}")
