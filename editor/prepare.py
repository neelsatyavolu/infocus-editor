"""Get a claimed edit's files into the job folder, work out what they are
(editor/recordings.py → show/recordings.json) and reject jobs that can't be a show
before any agent runs. Recordings are never joined into one file."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from editor.jobfiles import JobPaths, package_local_name, read_json
from editor.media import MediaError
from editor.portal import Claim
from editor.recordings import Candidate, DetectionError, Detector
from editor.sources import Source, SourceError, parse_sources

MIN_SHOW_S = 60
MAX_SHOW_S = 30 * 60


class JobFailed(Exception):
    """Ends the job as FAILED; the message is shown to producers."""


@dataclass(frozen=True)
class Prepared:
    recordings: Path  # show/recordings.json
    source: Path | None = None  # only when the job is exactly one program file
    decisions: tuple[dict[str, Any], ...] = field(default=())
    needs_review: bool = False


def check_length(seconds: float, what: str = "This") -> None:
    if not MIN_SHOW_S <= seconds <= MAX_SHOW_S:
        minutes, secs = divmod(int(seconds), 60)
        raise JobFailed(f"{what} doesn't look like a show recording (it runs {minutes}:{secs:02d}); "
                        "shows run between 1 and 30 minutes.")


async def download_all(drive, claim: Claim, paths: JobPaths, sources: tuple[Source, ...]) -> list[Path]:
    """Download sources (in brief order) then packages; files complete from an earlier attempt are kept."""
    packages = list(claim.brief.get("packages") or [])
    if len(claim.downloads) != len(sources) + len(packages):
        raise JobFailed(f"The Portal sent {len(claim.downloads)} files, but the job needs "
                        f"{len(sources) + len(packages)}.")
    local = []
    for i, d in enumerate(claim.downloads):
        if i < len(sources):
            dest = paths.src / f"{i + 1:02d}{Path(sources[i].path).suffix.lower()}"
            local.append(dest)
        else:
            dest = paths.packages / package_local_name(i - len(sources), d.path)
        if dest.is_file() and d.size is not None and dest.stat().st_size == d.size:
            continue
        await drive.download(d.url, dest, d.size)
    return local


async def prepare_edit(drive, claim: Claim, paths: JobPaths, run, *, envelope: Callable | None = None,
                       profile: Callable | None = None) -> Prepared:
    try:
        sources = parse_sources(claim.brief)
    except SourceError as e:
        raise JobFailed(str(e)) from e
    paths.root.mkdir(parents=True, exist_ok=True)
    # Files set to Ignore are never downloaded (they're absent from `downloads`); they're only reported.
    wanted = tuple(s for s in sources if s.role != "ignore")
    local = dict(zip(wanted, await download_all(drive, claim, paths, wanted)))
    candidates = [Candidate(local.get(s, Path(s.path)), Path(s.path).name, s.role, s.order, s.modified_at)
                  for s in sources]
    detector = Detector(paths.show, run=run, envelope=envelope, profile=profile)
    try:
        found = await asyncio.to_thread(detector.detect, candidates)
    except (DetectionError, MediaError) as e:
        raise JobFailed(str(e)) from e
    single = len(found.data["recordings"]) == 1 and len(found.data["recordings"][0]["files"]) == 1
    check_length(found.total_s, "This" if single else "Together, the recordings")
    return Prepared(found.path, found.source, found.decisions, found.needs_review)


def read_prepared(paths: JobPaths) -> Prepared | None:
    """For a fix: what the first run prepared, from its brief.json."""
    brief = read_json(paths.brief)
    if not brief or not brief.get("recordings") or not Path(brief["recordings"]).is_file():
        return None
    return Prepared(Path(brief["recordings"]), Path(brief["source"]) if brief.get("source") else None)


def touch(path: Path) -> None:
    """Mark a job folder as used, so cleanup keeps it 14 days from now."""
    os.utime(path)
