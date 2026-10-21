"""The launchd agent from the inside: restarting, and reloading a changed definition.

A running agent can't reload its own launchd definition (bootout would kill it
first), so after an update or repair that changed the plist, it starts a small
detached script that reloads the agent a moment after this process exits.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

LABEL = "com.infocus.editor"
EXIT_RESTART = 11
PLIST_FLAG = ".plist-changed"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def reload_if_changed(data_dir: Path, popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> bool:
    flag = data_dir / PLIST_FLAG
    if not flag.exists():
        return False
    flag.unlink(missing_ok=True)
    domain = f"gui/{os.getuid()}"
    script = (f'sleep 3; launchctl bootout {domain}/{LABEL} 2>/dev/null; '
              f'launchctl bootstrap {domain} "{plist_path()}"')
    # A new session, so it isn't in this job's process group and survives the bootout.
    popen(["/bin/sh", "-c", script], start_new_session=True, stdin=subprocess.DEVNULL,
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
