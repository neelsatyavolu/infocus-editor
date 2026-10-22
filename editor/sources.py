"""The files the Portal sent for a show (docs/multicam.md).

brief.sources = [{path, role, order}] with role auto | program | M | L | R | ignore.
Older Portals send only brief.sourcePaths, which means every file is `auto`. What the
files are (recordings, program or camera feeds) is detected from their content
(editor/recordings.py); a role other than `auto` overrides that for its file.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROLES = ("auto", "program", "M", "L", "R", "ignore")
EDGE_BYTES = 8 * 1024 * 1024
MAX_FILES = 12


class SourceError(Exception):
    """A plain reason the recording selection can't be edited."""


@dataclass(frozen=True)
class Source:
    path: str  # share-relative, e.g. "Shows/InFocus Show 08-26-2026 (1).mov"
    role: str
    order: int
    modified_at: float | None = None  # export finish time (epoch s), when the Portal sends it


def parse_sources(brief: Mapping[str, Any]) -> tuple[Source, ...]:
    raw = brief.get("sources")
    if isinstance(raw, list) and raw:
        sources = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict) or not item.get("path"):
                raise SourceError("The Portal sent a malformed recording list.")
            role = str(item.get("role") or "auto")
            if role not in ROLES:
                raise SourceError(f"Unknown recording role {role!r}.")
            sources.append(Source(str(item["path"]), role, int(item.get("order", i)), _epoch(item.get("modifiedAt"))))
        sources.sort(key=lambda s: s.order)
    else:
        sources = [Source(str(p), "auto", i) for i, p in enumerate(brief.get("sourcePaths") or [])]
    if not sources:
        raise SourceError("The job has no recording files.")
    if len(sources) > MAX_FILES:
        raise SourceError(f"A show can have at most {MAX_FILES} recording files.")
    return tuple(sources)


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def fingerprint(path: Path) -> tuple[int, str]:
    """Size plus the SHA-256 of the first and last 8 MB: equal means the same recording."""
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        digest.update(fh.read(EDGE_BYTES))
        if size > EDGE_BYTES:
            fh.seek(max(EDGE_BYTES, size - EDGE_BYTES))
            digest.update(fh.read(EDGE_BYTES))
    return size, digest.hexdigest()
