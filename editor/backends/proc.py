"""Run a CLI agent in its own process group, stream its stdout lines, and on
cancel stop the whole group (so an ffmpeg the agent started dies too)."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

STDERR_TAIL = 4000


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stderr_tail: str
    cancelled: bool = False


class ProcessRunner(Protocol):
    async def __call__(self, cmd: Sequence[str], *, env: Mapping[str, str], cwd: Path,
                       on_line: Callable[[str], None], cancel: asyncio.Event) -> ProcResult: ...


async def run_process(cmd: Sequence[str], *, env: Mapping[str, str], cwd: Path,
                      on_line: Callable[[str], None], cancel: asyncio.Event,
                      grace_s: float = 10.0) -> ProcResult:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), env=dict(env), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True, limit=16 * 1024 * 1024,
    )
    stderr_chunks: list[str] = []

    async def read_stdout() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            on_line(raw.decode("utf-8", "replace").rstrip("\n"))

    async def read_stderr() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            stderr_chunks.append(raw.decode("utf-8", "replace"))
            while sum(len(c) for c in stderr_chunks) > STDERR_TAIL and len(stderr_chunks) > 1:
                stderr_chunks.pop(0)

    readers = asyncio.ensure_future(asyncio.gather(read_stdout(), read_stderr()))
    exited = asyncio.ensure_future(_exit_code(proc))
    waiter = asyncio.ensure_future(cancel.wait())
    try:
        done, _ = await asyncio.wait({exited, waiter}, return_when=asyncio.FIRST_COMPLETED)
        cancelled = waiter in done and not exited.done()
        if cancelled:
            await stop_group(proc, grace_s)
        try:
            await asyncio.wait_for(asyncio.shield(readers), 5)
        except asyncio.TimeoutError:
            # Something the agent left running in the background still holds its output open.
            await stop_group(proc, 1.0)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(readers, 5)
        code = proc.returncode if proc.returncode is not None else -1
        return ProcResult(code, "".join(stderr_chunks), cancelled=cancelled)
    except asyncio.CancelledError:
        # The job's wall clock ran out (or the worker is stopping): take the group down.
        await stop_group(proc, grace_s)
        raise
    finally:
        waiter.cancel()
        if not readers.done():
            readers.cancel()


async def _exit_code(proc: asyncio.subprocess.Process) -> int:
    """The agent's exit, without waiting for its pipes to close (proc.wait() waits for those,
    so a background child holding stdout would make it hang)."""
    while proc.returncode is None:
        await asyncio.sleep(0.2)
    return proc.returncode


async def stop_group(proc: asyncio.subprocess.Process, grace_s: float = 10.0) -> None:
    """SIGINT, then SIGTERM, then SIGKILL to the agent's whole process group.

    Keeps going after the agent itself exits: background children (a shell's `ffmpeg &`
    ignores SIGINT) stay in the group and must die too.
    """
    pgid = proc.pid
    for sig, wait in ((signal.SIGINT, grace_s), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break  # the group is empty
        except PermissionError:
            break
        deadline = asyncio.get_running_loop().time() + wait
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
            if not _group_alive(pgid):
                break
        if not _group_alive(pgid):
            break
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_exit_code(proc), 5)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
