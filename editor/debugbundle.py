"""The debug bundle uploaded after every run: <job_dir>/logs/debug-<version>.tar.gz with
the job log, the agent transcripts and the small JSON files a person needs to see what
happened. Never media. Kept under 50 MB by cutting the middle out of the transcripts."""
from __future__ import annotations

import io
import json
import logging
import os
import tarfile
from pathlib import Path

from editor.joblog import LOG_NAME, LOGS
from editor.scrub import scrub

log = logging.getLogger(__name__)
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MIN_KEEP_BYTES = 64 * 1024
MEDIA_SUFFIXES = frozenset({".mov", ".mp4", ".m4v", ".mxf", ".wav", ".aif", ".aiff", ".m4a", ".aac", ".mp3",
                            ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".webp", ".exr"})
JOB_FILES = ("brief.json", "result.json", "status.json", "show/edl.json", "show/recordings.json",
             "show/layout.json", "show/check/verify.json")


def bundle_path(job_dir: Path, version: int) -> Path:
    return job_dir / LOGS / f"debug-{version}.tar.gz"


def _is_bundle(path: Path) -> bool:
    return path.name.startswith("debug-") and path.name.endswith(".tar.gz")


def _is_transcript(rel: str) -> bool:
    """The files whose middle can be cut to fit the cap: the agent transcripts and the job log."""
    name = rel.rsplit("/", 1)[-1]
    if rel == f"{LOGS}/{LOG_NAME}":
        return True
    return rel.startswith(f"{LOGS}/agent-") and (name.endswith(".jsonl") or name.endswith(".stderr.log"))


def collect(job_dir: Path) -> list[str]:
    """Paths (relative to the job folder) that go in the bundle, in a stable order."""
    picked: list[str] = []

    def add(path: Path, *, size_cap: bool) -> None:
        if not path.is_file() or path.suffix.lower() in MEDIA_SUFFIXES or path.name.startswith("."):
            return
        if size_cap and path.stat().st_size > MAX_FILE_BYTES:
            log.debug("debug bundle: skipping %s (over 5 MB)", path)
            return
        picked.append(path.relative_to(job_dir).as_posix())

    logs = job_dir / LOGS
    if logs.is_dir():
        for path in sorted(logs.rglob("*")):
            if not _is_bundle(path):
                add(path, size_cap=False)
    for rel in JOB_FILES:
        add(job_dir / rel, size_cap=True)
    analysis = job_dir / "show" / "analysis"
    if analysis.is_dir():
        for path in sorted(analysis.rglob("*")):
            if path.name == "report.md" or path.suffix.lower() == ".json":
                add(path, size_cap=True)
    return picked


def truncate_middle(data: bytes, keep: int, *, jsonl: bool) -> bytes:
    """The first and last lines, about `keep` bytes in all, with a marker line between."""
    if len(data) <= keep:
        return data
    lines = data.splitlines(keepends=True)
    head: list[bytes] = []
    tail: list[bytes] = []
    used = 0
    for line in lines:
        if used + len(line) > keep // 2:
            break
        head.append(line)
        used += len(line)
    used = 0
    for line in reversed(lines[len(head):]):
        if used + len(line) > keep // 2:
            break
        tail.append(line)
        used += len(line)
    tail.reverse()
    dropped = len(lines) - len(head) - len(tail)
    removed = len(data) - sum(map(len, head)) - sum(map(len, tail))
    note = f"{dropped} lines ({removed} bytes) cut here to keep the debug bundle under 50 MB"
    marker = (json.dumps({"type": "truncated", "note": note}) if jsonl else f"----- {note} -----") + "\n"
    return b"".join(head) + marker.encode() + b"".join(tail)


def _write(job_dir: Path, files: list[str], dest: Path, keep: int | None) -> None:
    tmp = dest.with_name(f".{dest.name}.tmp")
    with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
        for rel in files:
            path = job_dir / rel
            try:
                data = path.read_bytes()
                if keep is not None and _is_transcript(rel):
                    data = truncate_middle(data, keep, jsonl=rel.endswith(".jsonl"))
                # Logs are scrubbed when written; the job's JSON files (a producer's note) are scrubbed here.
                data = scrub(data.decode("utf-8", "replace")).encode("utf-8")
                info = tarfile.TarInfo(rel)
                info.size, info.mtime, info.mode = len(data), int(path.stat().st_mtime), 0o644
                tar.addfile(info, io.BytesIO(data))
            except OSError as e:  # a file the agent removed meanwhile
                log.debug("debug bundle: skipping %s: %s", rel, e)
    os.replace(tmp, dest)


def build_bundle(job_dir: Path, version: int, *, cap_bytes: int = MAX_BUNDLE_BYTES) -> Path:
    dest = bundle_path(job_dir, version)
    dest.parent.mkdir(parents=True, exist_ok=True)
    files = collect(job_dir)
    sizes = [(job_dir / rel).stat().st_size for rel in files if _is_transcript(rel)]
    keep: int | None = None
    _write(job_dir, files, dest, keep)
    while dest.stat().st_size > cap_bytes and sizes:
        keep = (keep or max(sizes)) // 2
        if keep < MIN_KEEP_BYTES:
            log.warning("debug bundle is still %d bytes with the logs cut down", dest.stat().st_size)
            break
        _write(job_dir, files, dest, keep)
    log.debug("debug bundle: %s, %d file(s), %d bytes%s", dest, len(files), dest.stat().st_size,
              f", transcripts cut to {keep} bytes each" if keep else "")
    return dest
