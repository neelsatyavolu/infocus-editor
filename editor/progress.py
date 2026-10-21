"""Forward job progress to the Portal: the runner's own stages plus the
skill's status.json, each change once, with a keepalive while nothing changes."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import AsyncIterator, Callable

from editor.jobfiles import read_json
from editor.portal import STAGES, AuthError

log = logging.getLogger(__name__)
KEEPALIVE_S = 300


class Reporter:
    """Owns the last stage sent for one job. Sets `cancel` when the Portal asks."""

    def __init__(self, portal, job_id: str, cancel: asyncio.Event, clock: Callable[[], float] = time.monotonic):
        self._portal = portal
        self._job_id = job_id
        self._cancel = cancel
        self._clock = clock
        self._last: tuple[str, str] | None = None
        self._sent_at = 0.0
        self.stage = "download"

    async def report(self, stage: str, message: str, *, force: bool = False) -> None:
        if stage not in STAGES:
            return
        key = (stage, message)
        if key == self._last and not force:
            return
        self._last = key
        self.stage = stage
        self._sent_at = self._clock()
        try:
            if await self._portal.progress(self._job_id, stage, message):
                self._cancel.set()
        except AuthError:
            raise
        except Exception as e:  # noqa: BLE001 - a missed progress report must not end the job
            log.warning("progress report failed: %s", e)

    async def keepalive(self) -> None:
        if self._last and self._clock() - self._sent_at >= KEEPALIVE_S:
            await self.report(*self._last, force=True)

    async def forward_status_file(self, status_path: Path) -> None:
        status = read_json(status_path)
        if status and status.get("stage"):
            await self.report(str(status["stage"]), str(status.get("message") or "")[:300])


@contextlib.asynccontextmanager
async def forwarding(reporter: Reporter, status_path: Path, poll_s: float = 5.0) -> AsyncIterator[None]:
    async def loop() -> None:
        while True:
            await reporter.forward_status_file(status_path)
            await reporter.keepalive()
            await asyncio.sleep(poll_s)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await reporter.forward_status_file(status_path)
