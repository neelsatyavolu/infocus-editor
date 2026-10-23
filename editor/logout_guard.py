"""Cancel normal macOS logout/shutdown while the dedicated editor service runs."""
from __future__ import annotations

import contextlib
import logging
import os
import select
import subprocess
from pathlib import Path
from typing import Iterator

from editor.config import ConfigError

log = logging.getLogger(__name__)
GUARD_PATH = Path("InFocus Editor Guard.app/Contents/MacOS/logout-guard")


def guard_available(data_dir: Path) -> bool:
    """The updater's doctor must exercise AppKit, not just check for a compiled binary."""
    try:
        result = subprocess.run([str(data_dir / GUARD_PATH), "--check"], capture_output=True, timeout=15)
        return result.returncode == 0 and result.stdout == b"ready\n"
    except (OSError, subprocess.TimeoutExpired):
        return False


@contextlib.contextmanager
def logout_guard(data_dir: Path, machine_kind: str, *, popen=subprocess.Popen,
                 select_ready=select.select) -> Iterator[subprocess.Popen | None]:
    if machine_kind != "dedicated":
        yield None
        return
    proc = None
    try:
        try:
            proc = popen([str(data_dir / GUARD_PATH), str(os.getpid())], stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE)
            ready, _, _ = select_ready([proc.stdout], [], [], 15)
            if not ready or proc.stdout.readline() != b"ready\n" or proc.poll() is not None:
                raise ConfigError("The logout/shutdown guard couldn't start. Open InFocus Editor Setup and choose Repair.")
        except OSError as e:
            raise ConfigError("The logout/shutdown guard is unavailable. Open InFocus Editor Setup and choose Repair.") from e
        log.info("Logout/shutdown protection is active for this dedicated editing Mac.")
        yield proc
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()  # SIGTERM bypasses AppKit's quit veto for intentional service stops.
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            proc.stdout.close()
