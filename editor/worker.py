"""The worker loop: heartbeat (health, machine, AI accounts, pairing key, acks),
Portal settings and commands, updates between jobs, one job at a time."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from editor.commands import IMMEDIATE, CommandAck, health_report
from editor.portal import AuthError, Claim, Command, Health
from editor.updater import CHECK_EVERY_S, RestartForUpdate, UpdateResult

log = logging.getLogger("editor")
UNLINKED = "This Mac isn't linked to the Portal — open InFocus Editor Setup and choose Link again."
STANDBY = "Linked, standby (another machine is active)."


class RestartRequested(Exception):
    """The Portal asked for a restart (or a repair finished): exit and let launchd start us again."""


@dataclass(frozen=True)
class LoopSettings:
    heartbeat_s: float = 60.0
    unlinked_heartbeat_s: float = 300.0
    poll_s: float = 30.0
    doctor_s: float = 600.0
    housekeeping_s: float = 3600.0
    update_s: float = CHECK_EVERY_S


class Worker:
    def __init__(self, *, portal, version: str, doctor: Callable[[], Health],
                 run_job: Callable[[Claim, asyncio.Event], Awaitable[Any]],
                 housekeeping: Callable[[], None] = lambda: None,
                 update: Callable[[str | None, tuple[str, ...]], UpdateResult] | None = None,
                 pairing: Any = None, backend_statuses: Callable[[], list[Any]] = lambda: [],
                 set_agent_order: Callable[[tuple[str, ...]], None] = lambda order: None,
                 commands: Any = None, machine: dict[str, str] | None = None,
                 before_exit: Callable[[], None] = lambda: None,
                 settings: LoopSettings = LoopSettings(), clock: Callable[[], float] = time.monotonic):
        self._portal = portal
        self._version = version
        self._doctor = doctor
        self._run_job = run_job
        self._housekeeping = housekeeping
        self._update = update
        self._pairing = pairing
        self._backend_statuses = backend_statuses
        self._set_agent_order = set_agent_order
        self._commands = commands
        self._machine = machine
        self._before_exit = before_exit
        self._s = settings
        self._clock = clock
        self.health = Health(False, ("Starting up",))
        self._health_at = float("-inf")
        self._housekeeping_at = float("-inf")
        self._update_at = float("-inf")
        self.busy = False
        self.current: tuple[str, asyncio.Event] | None = None
        self.target_version: str | None = None
        self.update_problem: str | None = None
        self.unlinked = False
        self.active = True
        self._acks: list[dict[str, Any]] = []
        self._command_acks: list[dict[str, Any]] = []
        self._queued: list[Command] = []
        self._seen_commands: set[str] = set()

    # ---- health -----------------------------------------------------------

    async def refresh_health(self, force: bool = False) -> Health:
        if force or self._clock() - self._health_at >= self._s.doctor_s:
            try:
                self.health = await asyncio.to_thread(self._doctor)
            except Exception as e:  # noqa: BLE001
                self.health = Health(False, (f"The health check crashed: {e}",))
            self._health_at = self._clock()
        return self.health

    def reported_health(self) -> Health:
        problems = list(self.health.problems)
        if self.update_problem:
            problems.append(self.update_problem)
        if self.unlinked:
            return Health(False, (UNLINKED, *problems))
        return Health(self.health.ok, tuple(problems))

    def status_lines(self) -> tuple[str, ...]:
        if self.unlinked:
            return (UNLINKED,)
        return () if self.active else (STANDBY,)

    # ---- heartbeat --------------------------------------------------------

    async def heartbeat_once(self) -> None:
        acks, self._acks = self._acks[:10], self._acks[10:]
        command_acks, self._command_acks = self._command_acks[:10], self._command_acks[10:]
        try:
            reply = await self._portal.heartbeat(
                version=self._version, busy=self.busy, health=self.reported_health(),
                public_key=self._pairing.public_key if self._pairing else None,
                backends=[s.as_json() for s in self._backend_statuses()], acks=acks,
                command_acks=command_acks, machine=self._machine)
        except AuthError:
            self._acks, self._command_acks = acks + self._acks, command_acks + self._command_acks
            if not self.unlinked:
                log.warning("%s", UNLINKED)
            self.unlinked = True
            return
        except Exception:
            self._acks, self._command_acks = acks + self._acks, command_acks + self._command_acks
            raise
        if self.unlinked:
            log.info("linked to the Portal again")
        self.unlinked = False
        if reply.active != self.active:
            log.info("%s", "This Mac is the active editor." if reply.active else STANDBY)
        self.active = reply.active
        if reply.target_version != self.target_version:
            self.target_version = reply.target_version
            self._update_at = float("-inf")  # a new pin is checked at the next idle moment
        if reply.agent_order:
            self._set_agent_order(reply.agent_order)
        if self.current and self.current[0] in reply.cancel_job_ids:
            log.info("cancel requested for %s", self.current[0])
            self.current[1].set()
        if reply.credentials and self._pairing:
            await self._install_credentials(reply.credentials)
        for command in reply.commands:
            if command.id in self._seen_commands:
                continue
            self._seen_commands.add(command.id)
            if command.kind in IMMEDIATE:
                await self._run_immediate(command)
            else:
                self._queued.append(command)

    async def _install_credentials(self, items) -> None:
        from editor.pairing import parse_credentials

        acks = await asyncio.to_thread(self._pairing.install_all, parse_credentials(items))
        for ack in acks:
            log.info("credential %s: %s", ack.id, "installed" if ack.ok else ack.error)
        self._acks += [a.as_json() for a in acks]
        await self.refresh_health(force=True)

    async def heartbeat_forever(self) -> None:
        while True:
            try:
                await self.heartbeat_once()
            except Exception as e:  # noqa: BLE001 - keep beating through network blips
                log.warning("heartbeat failed: %s", e)
            await asyncio.sleep(self._s.unlinked_heartbeat_s if self.unlinked else self._s.heartbeat_s)

    # ---- commands ---------------------------------------------------------

    def _ack(self, command: Command, ok: bool, output: str = "") -> None:
        log.info("command %s (%s): %s", command.id, command.kind, "ok" if ok else "failed")
        self._command_acks.append(CommandAck(command.id, ok, output).as_json())

    async def _run_immediate(self, command: Command) -> None:
        if command.kind == "doctor":
            await self.refresh_health(force=True)
            self._ack(command, True, health_report(self.reported_health(), self.status_lines()))
        elif command.kind == "logs" and self._commands is not None:
            self._ack(command, True, await asyncio.to_thread(self._commands.logs))
        else:
            self._ack(command, False, "This editor can't run that command.")

    async def run_queued_commands(self) -> None:
        """Between jobs only: update, restart, repair."""
        while self._queued and not self.busy:
            command = self._queued.pop(0)
            if command.kind == "update":
                await self._command_update(command)
            elif command.kind == "restart":
                self._ack(command, True, "Restarting.")
                await self._exit_after_ack(RestartRequested("restart requested from the Portal"))
            elif command.kind == "repair" and self._commands is not None:
                ok, output = await asyncio.to_thread(self._commands.repair)
                self._ack(command, ok, output)
                if ok:
                    await self._exit_after_ack(RestartRequested("repair finished"))
            else:
                self._ack(command, False, "This editor can't run that command.")

    async def _command_update(self, command: Command) -> None:
        if self._update is None:
            self._ack(command, False, f"Automatic updates are off on this checkout ({self._version}).")
            return
        self._update_at = self._clock()
        result = await asyncio.to_thread(self._update, self.target_version, self.health.problems)
        if result.status == "updated":
            self._ack(command, True, result.message)
            await self._exit_after_ack(RestartForUpdate(result.message))
        self.update_problem = result.problem or self.update_problem
        self._ack(command, result.status == "current", result.message or f"Already on {self._version}.")

    async def _exit_after_ack(self, exc: Exception) -> None:
        with contextlib.suppress(Exception):
            await self.heartbeat_once()  # deliver the ack before going away
        with contextlib.suppress(Exception):
            self._before_exit()
        raise exc

    # ---- updates and jobs -------------------------------------------------

    async def maybe_update(self) -> None:
        """Only ever called between jobs. Raises RestartForUpdate after a successful update."""
        if self._update is None or self.busy or self._clock() - self._update_at < self._s.update_s:
            return
        self._update_at = self._clock()
        result = await asyncio.to_thread(self._update, self.target_version, self.health.problems)
        log.info("update check: %s %s", result.status, result.message)
        if result.status == "updated":
            with contextlib.suppress(Exception):
                self._before_exit()
            raise RestartForUpdate(result.message)
        if result.status in ("current", "rolled_back", "refused"):
            self.update_problem = result.problem

    async def step(self) -> bool:
        """One pass of the claim loop. Returns True if a job ran."""
        if self.unlinked:
            return False
        await self.run_queued_commands()
        await self.maybe_update()
        if self._clock() - self._housekeeping_at >= self._s.housekeeping_s:
            self._housekeeping_at = self._clock()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._housekeeping)
        health = await self.refresh_health()
        if not health.ok:
            return False
        # Always ask, active or not: the Portal decides what an inactive machine gets (its own fixes).
        claim = await self._portal.claim()
        if claim is None:
            return False
        cancel = asyncio.Event()
        self.busy, self.current = True, (claim.job_id, cancel)
        started = time.monotonic()
        try:
            log.info("job %s started: %s v%s for %s", claim.job_id, claim.kind, claim.version, claim.show_date)
            report = await self._run_job(claim, cancel)
            log.info("job %s finished: %s in %.0f s", claim.job_id, getattr(report, "status", report),
                     time.monotonic() - started)
        except BaseException as e:
            log.error("job %s ended with %s after %.0f s", claim.job_id, e.__class__.__name__,
                      time.monotonic() - started)
            raise
        finally:
            self.busy, self.current = False, None
        await self.refresh_health(force=True)
        return True

    async def serve(self) -> None:
        await self.refresh_health(force=True)
        beat = asyncio.create_task(self.heartbeat_forever())
        try:
            while True:
                try:
                    ran = await self.step()
                except AuthError:
                    # Unlinked mid-way: stop claiming, keep heartbeating every 5 minutes. Never exit-loop.
                    if not self.unlinked:
                        log.warning("%s", UNLINKED)
                    self.unlinked = True
                    ran = False
                except (RestartForUpdate, RestartRequested):
                    raise
                except Exception as e:  # noqa: BLE001
                    log.exception("loop error: %s", e)
                    ran = False
                if not ran:
                    await asyncio.sleep(self._s.poll_s)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
