"""Keeping the Mac awake. A dedicated editing Mac runs the whole service under
`caffeinate -dimsu` (launchd plist). A personal computer keeps its normal sleep
settings, and the runner holds an idle-sleep assertion only while a job runs."""
from __future__ import annotations

import contextlib
import os
import subprocess
from typing import Callable, Iterator

Popen = Callable[..., subprocess.Popen]


@contextlib.contextmanager
def job_awake(machine_kind: str, popen: Popen = subprocess.Popen) -> Iterator[None]:
    """On a personal computer, `caffeinate -i -w <worker pid>` for the length of a job.
    `-w` also ends it if the worker dies, so it can't keep the Mac awake forever."""
    if machine_kind != "personal":
        yield
        return
    proc = None
    try:
        proc = popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        proc = None  # no caffeinate: the job still runs, the Mac may sleep
    try:
        yield
    finally:
        if proc is not None:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
