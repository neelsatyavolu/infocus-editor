"""The recent log for the Portal's `logs` command, with secrets scrubbed, and the
global log's own settings."""
from __future__ import annotations

import logging
from collections import deque
from pathlib import Path

from editor.scrub import scrub

__all__ = ["LOG_FILE", "MAX_OUTPUT", "latest_job_log", "logs_report", "quiet_http_logs", "scrub", "tail_log"]

LOG_FILE = Path.home() / "Library" / "Logs" / "infocus-editor.log"
MAX_OUTPUT = 4000
GLOBAL_LINES = 100
JOB_LINES = 150


def _last_lines(path: Path, lines: int) -> list[str] | None:
    try:
        with path.open("r", errors="replace") as fh:
            return list(deque(fh, maxlen=lines))
    except OSError:
        return None


def tail_log(path: Path = LOG_FILE, lines: int = GLOBAL_LINES) -> str:
    last = _last_lines(path, lines)
    if last is None:
        return f"No log at {path}."
    return scrub("".join(last))[-MAX_OUTPUT:]


def latest_job_log(work_root: Path) -> Path | None:
    """The editor.log of the job folder that logged most recently."""
    newest: tuple[float, Path] | None = None
    try:
        candidates = list(work_root.glob("*/logs/editor.log"))
    except OSError:
        return None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return newest[1] if newest else None


def _fit(lines: list[str], budget: int) -> str:
    """The most recent whole lines that fit in `budget` characters."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > budget:
            break
        kept.append(line)
        used += len(line)
    return "".join(reversed(kept))


def logs_report(global_log: Path = LOG_FILE, work_root: Path | None = None, limit: int = MAX_OUTPUT) -> str:
    """The global log tail, then the latest job's log tail, scrubbed and within `limit` characters.
    Trimming drops the oldest lines of each part first."""
    last = _last_lines(global_log, GLOBAL_LINES)
    global_lines = [f"No log at {global_log}.\n"] if last is None else scrub("".join(last)).splitlines(keepends=True)
    job = latest_job_log(work_root) if work_root else None
    if job is None:
        return _fit(global_lines, limit)
    header = f"\n----- latest job: {job.parent.parent.name} -----\n"
    job_lines = scrub("".join(_last_lines(job, JOB_LINES) or [])).splitlines(keepends=True)
    room = limit - len(header)
    global_need, job_need = sum(map(len, global_lines)), sum(map(len, job_lines))
    # Split the room evenly; whatever one part doesn't need goes to the other.
    global_budget = min(global_need, max(room // 2, room - job_need))
    global_text = _fit(global_lines, global_budget).rstrip("\n")
    return (global_text + header + _fit(job_lines, room - global_budget))[-limit:]


class _HttpxFailuresOnly(logging.Filter):
    """httpx logs every request at INFO ("HTTP Request: POST … 200 OK"): keep only failures."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        args = record.args if isinstance(record.args, tuple) else ()
        status = next((a for a in args if isinstance(a, int)), None)
        return status is not None and status >= 400


def quiet_http_logs() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, _HttpxFailuresOnly) for f in logger.filters):
        logger.addFilter(_HttpxFailuresOnly())
    logging.getLogger("httpcore").setLevel(logging.WARNING)
