"""Old job folders and leftover job processes."""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

Run = Callable[..., subprocess.CompletedProcess]
DAY_S = 86400


def cleanup_job_dirs(work_root: Path, keep_days: int, now: float | None = None) -> list[Path]:
    """Delete job folders untouched for more than keep_days."""
    now = time.time() if now is None else now
    removed = []
    if not work_root.is_dir():
        return removed
    for child in work_root.iterdir():
        if child.is_dir() and now - child.stat().st_mtime > keep_days * DAY_S:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child)
    return removed


def kill_job_processes(job_dir: Path, run: Run = subprocess.run, sleep: Callable[[float], None] = time.sleep) -> list[int]:
    """Stop anything still working on a job's files (e.g. an ffmpeg an agent started)."""
    import os
    import signal

    proc = run(["pgrep", "-f", str(job_dir)], capture_output=True, text=True, check=False)
    pids = [int(p) for p in (proc.stdout or "").split() if p.isdigit() and int(p) != os.getpid()]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = []
        for pid in pids:
            try:
                os.kill(pid, sig)
                alive.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        if not alive or sig == signal.SIGKILL:
            break
        sleep(3)
        pids = alive
    return pids
