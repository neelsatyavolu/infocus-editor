"""Health check: everything an unattended edit needs, as plain-sentence problems.

Blocking problems stop the worker from claiming jobs. Warnings ("Warning: …") are
reported but don't block.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from editor.config import Config
from editor.portal import Health

MIN_FREE_GB = 60
WHISPER_MODEL = Path.home() / ".cache" / "whisper-cpp" / "ggml-large-v3-turbo.bin"
PLAYWRIGHT_CACHE = Path.home() / "Library" / "Caches" / "ms-playwright"
ROOT_MARKER = Path("/Library/InFocusEditor/bootstrap.done")
ROOT_HELPER = Path("/Library/InFocusEditor/bin/infocus-root")
SUDOERS_FILE = Path("/etc/sudoers.d/infocus-editor")
ADMIN_STEP_PROBLEM = ("The one-time admin step is missing or out of date. "
                      "Run install/install.sh; an admin needs to approve once.")
UNLINKED_PROBLEM = "This Mac isn't linked to the Portal — open InFocus Editor Setup and choose Link again."
NO_BACKEND_PROBLEM = "No AI account connected: in the Portal open Automatic editing → Connect AI account."
VIDEOTOOLBOX_WARNING = ("Warning: this ffmpeg has no h264_videotoolbox encoder, so renders use the slower "
                        "libx264 fallback.")
TOOLS = {
    "uv": "install uv (https://docs.astral.sh/uv/)",
    "ffmpeg": "run `brew install ffmpeg`",
    "ffprobe": "run `brew install ffmpeg`",
    "node": "run `brew install node`",
    "whisper-cli": "run `brew install whisper-cpp`",
    "swiftc": "install the Xcode command line tools (run install/install.sh)",
    "git": "install the Xcode command line tools (run install/install.sh)",
}
INTRO_OUTRO = ("03 Intro & Outro/InFocus Intro 2026.mp4", "03 Intro & Outro/InFocus Outro 2026.mp4")


def _ffmpeg_encoders() -> str:
    try:
        return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True,
                              check=False, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


@dataclass(frozen=True)
class Probes:
    which: Callable[[str], str | None] = shutil.which
    exists: Callable[[Path], bool] = Path.exists
    free_bytes: Callable[[Path], int] = lambda p: shutil.disk_usage(_existing(p)).free
    power_source: Callable[[], str] = lambda: subprocess.run(
        ["pmset", "-g", "batt"], capture_output=True, text=True, check=False).stdout
    playwright_ok: Callable[[], bool] = lambda: any(PLAYWRIGHT_CACHE.glob("chromium*"))
    admin_step_ok: Callable[[Path], bool] = lambda repo: admin_step_ok(repo)
    ffmpeg_encoders: Callable[[], str] = _ffmpeg_encoders
    connected_backends: Callable[[], list[str]] = lambda: []
    linked: Callable[[], bool] = lambda: True


def _existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def helper_version(path: Path) -> str | None:
    try:
        for line in path.read_text().splitlines():
            if line.startswith("HELPER_VERSION="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def admin_step_ok(repo_root: Path) -> bool:
    """The root-owned marker, helper (at the repo's version) and sudoers rule exist and work."""
    try:
        if ROOT_MARKER.stat().st_uid != 0 or ROOT_HELPER.stat().st_uid != 0 or not SUDOERS_FILE.exists():
            return False
    except OSError:
        return False
    if helper_version(ROOT_HELPER) != helper_version(repo_root / "install" / "infocus-root"):
        return False
    proc = subprocess.run(["sudo", "-n", str(ROOT_HELPER), "status"], capture_output=True, check=False, timeout=15)
    return proc.returncode == 0


def check(cfg: Config, probes: Probes = Probes()) -> Health:
    blocking: list[str] = []
    warnings: list[str] = []
    if not probes.linked():
        blocking.append(UNLINKED_PROBLEM)
    if not probes.admin_step_ok(cfg.repo_root):
        blocking.append(ADMIN_STEP_PROBLEM)
    if not probes.connected_backends():
        blocking.append(NO_BACKEND_PROBLEM)
    blocking += _tools(probes)
    if probes.which("ffmpeg") and "h264_videotoolbox" not in probes.ffmpeg_encoders():
        warnings.append(VIDEOTOOLBOX_WARNING)
    if not probes.exists(WHISPER_MODEL):
        blocking.append(f"The whisper model is missing: run install/install.sh to download it to {WHISPER_MODEL.parent}.")
    if not probes.playwright_ok():
        blocking.append("Playwright's Chromium is missing: run install/install.sh.")
    blocking += _package(cfg, probes)
    free_gb = probes.free_bytes(cfg.work_root) / 1024**3
    if free_gb < MIN_FREE_GB:
        blocking.append(f"Only {free_gb:.0f} GB free for job folders; at least {MIN_FREE_GB} GB is needed.")
    if "AC Power" not in probes.power_source():
        blocking.append("The MacBook is on battery; plug it in.")
    return Health(ok=not blocking, problems=tuple(blocking + warnings))


def _tools(probes: Probes) -> Iterable[str]:
    for tool, fix in TOOLS.items():
        if probes.which(tool) is None:
            yield f"{tool} is missing: {fix}."


def _package(cfg: Config, probes: Probes) -> list[str]:
    if not probes.exists(cfg.package_dir):
        return [f"The InFocus graphics package folder is missing ({cfg.package_dir}); set `package_dir` in config.toml."]
    missing = [rel for rel in INTRO_OUTRO if not probes.exists(cfg.package_dir / rel)]
    return [f"The package is missing {rel}." for rel in missing]
