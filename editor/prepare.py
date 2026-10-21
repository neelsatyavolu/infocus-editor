"""Get a claimed edit's files into the job folder and reject recordings that
can't be a show before any Claude cost."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from editor.jobfiles import JobPaths, package_local_name
from editor.media import MediaError, MediaInfo, join_sources, probe
from editor.portal import Claim

MIN_SHOW_S = 60
MAX_SHOW_S = 30 * 60


class JobFailed(Exception):
    """Ends the job as FAILED; the message is shown to producers."""


def precheck(info: MediaInfo) -> None:
    if not info.has_video:
        raise JobFailed("This file has no video; pick the show recording.")
    if info.width <= 768 and info.height <= 576:
        raise JobFailed("This is the SD capture; pick the HD recording.")
    if not MIN_SHOW_S <= info.duration <= MAX_SHOW_S:
        minutes, seconds = divmod(int(info.duration), 60)
        raise JobFailed(f"This doesn't look like a show recording (it runs {minutes}:{seconds:02d}); "
                        "shows run between 1 and 30 minutes.")


async def download_all(drive, claim: Claim, paths: JobPaths) -> list[Path]:
    """Download sources then packages (the Portal sends them in that order).
    Files already complete from an earlier attempt are kept."""
    source_paths = list(claim.brief.get("sourcePaths") or [])
    packages = list(claim.brief.get("packages") or [])
    if len(claim.downloads) != len(source_paths) + len(packages):
        raise JobFailed(f"The Portal sent {len(claim.downloads)} files, but the job needs "
                        f"{len(source_paths) + len(packages)}.")
    sources = []
    for i, d in enumerate(claim.downloads):
        if i < len(source_paths):
            dest = paths.src / f"{i + 1:02d}{Path(d.path).suffix.lower()}"
            sources.append(dest)
        else:
            dest = paths.packages / package_local_name(i - len(source_paths), d.path)
        if dest.is_file() and d.size is not None and dest.stat().st_size == d.size:
            continue
        await drive.download(d.url, dest, d.size)
    return sources


async def prepare_edit(drive, claim: Claim, paths: JobPaths, run) -> Path:
    paths.root.mkdir(parents=True, exist_ok=True)
    sources = await download_all(drive, claim, paths)
    try:
        source = await asyncio.to_thread(join_sources, sources, paths.root, run)
        info = await asyncio.to_thread(probe, source, run)
    except MediaError as e:
        raise JobFailed(str(e)) from e
    precheck(info)
    return source


def touch(path: Path) -> None:
    """Mark a job folder as used, so cleanup keeps it 14 days from now."""
    os.utime(path)
