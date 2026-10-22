"""Commands producers queue from the Portal: doctor, update, restart, repair, logs, always-on, normal-power.
This module does the work; editor/main.py decides when (doctor and logs any time,
the rest only between jobs)."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from editor.logs import LOG_FILE, MAX_OUTPUT, logs_report, scrub
from editor.portal import Health

REPAIR_TIMEOUT_S = 45 * 60
IMMEDIATE = ("doctor", "logs", "always-on", "normal-power")  # safe while a job runs
ROOT_HELPER = "/Library/InFocusEditor/bin/infocus-root"
POWER_STATES = {"always-on": "on", "normal-power": "off"}


@dataclass(frozen=True)
class CommandAck:
    id: str
    ok: bool
    output: str = ""

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.output:
            out["output"] = self.output[-MAX_OUTPUT:]
        return out


def health_report(health: Health, status_lines: tuple[str, ...] = ()) -> str:
    lines = list(status_lines)
    lines += [f"✗ {p}" for p in health.problems]
    lines.append("Healthy: ready to edit." if health.ok else f"{sum(not p.startswith('Warning') for p in health.problems)} problem(s).")
    return "\n".join(lines)


class CommandRunner:
    def __init__(self, repo_dir: Path, data_dir: Path, *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 log_tail: Callable[[], str] | None = None, work_root: Path | None = None,
                 log_file: Path = LOG_FILE):
        self._repo = repo_dir
        self._data = data_dir
        self._run = run
        # The global log tail, then the tail of the latest job's own log.
        self._log_tail = log_tail or (lambda: logs_report(log_file, work_root))

    def logs(self) -> str:
        return self._log_tail()

    def set_power(self, kind: str) -> tuple[bool, str]:
        """always-on: the Mac never sleeps (lid closed or open, adapter or battery), nothing logs it out and macOS
        doesn't restart it for updates. normal-power: back to macOS's own settings. Through the root helper."""
        try:
            proc = self._run(["/usr/bin/sudo", "-n", ROOT_HELPER, "always-on", POWER_STATES[kind]],
                             capture_output=True, text=True, check=False, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"Couldn't run the admin helper: {e.__class__.__name__}"
        if proc.returncode != 0:
            why = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
            return False, ("The admin helper refused (an admin may need to approve the newest version: open "
                           f"InFocus Editor Setup and choose Repair). {why[0]}")
        state = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        if kind == "always-on":
            return True, ("Always on: this Mac won't sleep (even with the lid closed or on battery), restarts after "
                          "a power cut, never logs out by itself and won't restart for macOS updates. "
                          f"(sleep disabled: {state.get('always_on', '?')})")
        return True, f"Normal power: macOS's own sleep and update settings are back. (sleep disabled: {state.get('always_on', '?')})"

    def repair(self) -> tuple[bool, str]:
        """Re-run setup in its unattended update mode: reinstall missing tools and dependencies."""
        cmd = [str(self._repo / "install" / "user-setup.sh"), "--update", "--unattended", "--home", str(self._data)]
        try:
            proc = self._run(cmd, capture_output=True, text=True, check=False, timeout=REPAIR_TIMEOUT_S,
                             env={**os.environ, "INFOCUS_EDITOR_HOME": str(self._data)})
        except subprocess.TimeoutExpired:
            return False, "Repair timed out after 45 minutes."
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-40:])
        return proc.returncode == 0, scrub(tail)
