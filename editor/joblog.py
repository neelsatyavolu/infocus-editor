"""The per-job debug log and agent transcripts, all under <job_dir>/logs/:

- editor.log: every runner step at DEBUG, one `----- run <n> (<kind>, v<version>) -----`
  header per run (edits, reruns and fixes of the same job folder share the file)
- agent-<NN>-<backend>.jsonl: one session's raw event stream, numbered per job folder
- agent-<NN>-<backend>.stderr.log: a CLI agent's stderr

Everything is scrubbed (editor/scrub.py) on the way to disk, and a logging problem never
breaks the job."""
from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Iterator, Mapping

from editor.scrub import scrub

log = logging.getLogger(__name__)
LOGS = "logs"
LOG_NAME = "editor.log"
FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
CLIP = 300
_RUN_HEADER = re.compile(r"^----- run (\d+) ")
_SESSION = re.compile(r"^agent-(\d+)-")


def logs_dir(job_dir: Path) -> Path:
    return job_dir / LOGS


def clip(value: Any, limit: int = CLIP) -> str:
    """One line of at most `limit` characters, for summaries of commands and outputs."""
    text = value if isinstance(value, str) else json.dumps(value, default=repr, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---- editor.log ---------------------------------------------------------------


class ScrubFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


class _QuietFileHandler(logging.FileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        """A full disk or a deleted job folder must never break the job."""


@dataclass
class _Attached:
    handler: logging.Handler
    editor_level: int
    pinned: list[logging.Handler]


def _next_run(path: Path) -> int:
    try:
        with path.open("r", errors="replace") as fh:
            return sum(1 for line in fh if _RUN_HEADER.match(line)) + 1
    except OSError:
        return 1


def _attach(job_dir: Path, kind: str, version: int) -> _Attached | None:
    try:
        folder = logs_dir(job_dir)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / LOG_NAME
        run = _next_run(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if fh.tell() else "") + f"----- run {run} ({kind}, v{version}) -----\n")
        handler = _QuietFileHandler(path, encoding="utf-8")
    except OSError as e:
        log.warning("couldn't open the job log in %s: %s", job_dir, e)
        return None
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(ScrubFormatter(FORMAT, DATEFMT))
    editor, root = logging.getLogger("editor"), logging.getLogger()
    before = editor.getEffectiveLevel()
    # The editor's DEBUG lines go to the job log only: handlers that took everything keep their old level.
    pinned = [h for h in root.handlers if h.level == logging.NOTSET]
    for h in pinned:
        h.setLevel(before)
    attached = _Attached(handler, editor.level, pinned)
    editor.setLevel(logging.DEBUG)
    root.addHandler(handler)
    return attached


def _detach(attached: _Attached | None) -> None:
    if attached is None:
        return
    root = logging.getLogger()
    with contextlib.suppress(Exception):
        root.removeHandler(attached.handler)
        attached.handler.close()
    logging.getLogger("editor").setLevel(attached.editor_level)
    for h in attached.pinned:
        h.setLevel(logging.NOTSET)


@contextlib.contextmanager
def job_log(job_dir: Path, kind: str, version: int) -> Iterator[None]:
    """While the block runs, the root logger also writes <job_dir>/logs/editor.log at DEBUG."""
    attached = _attach(job_dir, kind, version)
    try:
        yield
    finally:
        _detach(attached)


# ---- transcripts ----------------------------------------------------------------


def _next_session(folder: Path) -> int:
    numbers = [int(m.group(1)) for p in folder.iterdir() if (m := _SESSION.match(p.name))]
    return max(numbers, default=0) + 1


class Transcript:
    """The raw event stream of one agent session. Writes are scrubbed and never raise."""

    def __init__(self, job_dir: Path, backend: str):
        self.path: Path | None = None
        self.stderr_path: Path | None = None
        self._out: IO[str] | None = None
        self._err: IO[str] | None = None
        try:
            folder = logs_dir(job_dir)
            folder.mkdir(parents=True, exist_ok=True)
            for _ in range(5):
                path = folder / f"agent-{_next_session(folder):02d}-{backend}.jsonl"
                try:
                    self._out = path.open("x", encoding="utf-8", buffering=1)  # reserves the number
                except FileExistsError:
                    continue
                self.path, self.stderr_path = path, path.with_suffix(".stderr.log")
                break
        except OSError as e:
            log.warning("couldn't start the %s transcript: %s", backend, e)
        if self.path:
            log.debug("transcript: %s", self.path)

    def line(self, raw: str) -> None:
        """One event exactly as the agent printed it (scrubbed)."""
        self._write(self._out, scrub(raw.rstrip("\n")) + "\n")

    def event(self, kind: str, data: Mapping[str, Any]) -> None:
        self.line(json.dumps({"type": kind, **data}, default=repr, ensure_ascii=False))

    def stderr(self, text: str) -> None:
        if self._err is None and self.stderr_path is not None:
            try:
                self._err = self.stderr_path.open("a", encoding="utf-8", buffering=1)
            except OSError:
                self.stderr_path = None
        self._write(self._err, scrub(text if text.endswith("\n") else text + "\n"))

    def _write(self, fh: IO[str] | None, text: str) -> None:
        if fh is None:
            return
        with contextlib.suppress(OSError, ValueError):
            fh.write(text)

    def close(self) -> None:
        for fh in (self._out, self._err):
            if fh is not None:
                with contextlib.suppress(OSError):
                    fh.close()
        self._out = self._err = None

    def __enter__(self) -> "Transcript":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---- one-line summaries for the runner ---------------------------------------------


def describe_claim(claim: Any) -> str:
    """Job id, show date, kind, version, how many sources and packages. Never URLs or tokens."""
    brief = claim.brief
    sources = brief.get("sources") or brief.get("sourcePaths") or []
    ignored = sum(1 for s in sources if isinstance(s, dict) and s.get("role") == "ignore")
    text = (f"claim: job {claim.job_id}, show {claim.show_date}, {claim.kind} v{claim.version}, "
            f"{len(sources)} source(s){f' ({ignored} ignored)' if ignored else ''}, "
            f"{len(brief.get('packages') or [])} package(s), {len(claim.downloads)} download(s), "
            f"filmed {brief.get('filmedOn') or '?'}, note {'yes' if brief.get('note') else 'no'}")
    if claim.fix:
        text += (f"; fix: remember={claim.fix.remember}, session {claim.fix.agent_session_id or 'none'}, "
                 f"request: {clip(claim.fix.text, 200)}")
    return text


def describe_recordings(data: Mapping[str, Any]) -> list[str]:
    lines = []
    for rec in data.get("recordings") or []:
        files = ", ".join(f"{Path(str(f.get('path'))).name}={f.get('role')}@{f.get('offsetFrames')}"
                          for f in rec.get("files") or [])
        lines.append(f"recording {rec.get('id')}: {rec.get('kind')}, {rec.get('frames')} frames, "
                     f"confidence {rec.get('confidence')}, files {files}")
    for skipped in data.get("skipped") or []:
        lines.append(f"skipped: {clip(skipped)}")
    return lines


def describe_payload(payload: Mapping[str, Any]) -> str:
    decisions = payload.get("decisions") or []
    checks = payload.get("checks") or {}
    rules = sorted({str(f.get("rule")) for f in checks.get("failures") or [] if isinstance(f, dict)})
    return (f"needsReview={payload.get('needsReview')}, coversClean={checks.get('coversClean')}, "
            f"failures={rules or 'none'}, {len(decisions)} decision(s) "
            f"({sum(1 for d in decisions if d.get('flagged'))} flagged), cost ${payload.get('costUsd', 0)}, "
            f"session {payload.get('agentSessionId') or 'none'}, output {payload.get('outputNasPath')}, "
            f"debugLogPath {payload.get('debugLogPath') or 'none'}"
            + (f", reply: {clip(payload['reply'], 200)}" if payload.get("reply") else ""))
