"""Commands producers queue from the Portal: doctor, update, restart, repair, logs.
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
IMMEDIATE = ("doctor", "logs")  # safe while a job runs


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
