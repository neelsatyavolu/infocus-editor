"""Client for the Portal's /api/service/show-editor/ routes (docs/portal-api.md)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

import httpx

STAGES = ("download", "analyze", "graphics", "audio", "build", "render", "check", "upload", "fix")
RETRIES = 3
BACKOFF_S = (1.0, 3.0, 9.0)

Sleep = Callable[[float], Awaitable[None]]


class AuthError(Exception):
    """The Portal rejected the editor token (401). Fatal: the worker stops."""


class PortalError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Download:
    path: str
    url: str = field(repr=False)  # carries a file token
    size: int | None = None


@dataclass(frozen=True)
class Fix:
    text: str
    remember: bool
    agent_session_id: str | None
    version: int


@dataclass(frozen=True)
class Claim:
    kind: str  # "edit" | "fix"
    job_id: str
    show_date: str
    version: int
    brief: Mapping[str, Any]
    downloads: tuple[Download, ...] = ()
    fix: Fix | None = None


@dataclass(frozen=True)
class UploadTarget:
    path: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class UploadTargets:
    drive_base_url: str
    video: UploadTarget
    poster: UploadTarget
    log: UploadTarget | None = None  # the debug bundle; older Portals don't send it


@dataclass(frozen=True)
class Command:
    id: str
    kind: str  # doctor | update | restart | repair | logs | always-on | normal-power | shell:<signed payload>


COMMAND_KINDS = ("doctor", "update", "restart", "repair", "logs", "always-on", "normal-power")


@dataclass(frozen=True)
class HeartbeatReply:
    cancel_job_ids: tuple[str, ...] = ()
    target_version: str | None = None
    credentials: tuple[Mapping[str, Any], ...] = ()  # [{id, backend, ciphertext}], sealed to our key
    agent_order: tuple[str, ...] | None = None  # the Portal's settings.agentOrder, if set
    commands: tuple[Command, ...] = ()
    active: bool = True  # False: another linked machine is the active editor


@dataclass(frozen=True)
class Health:
    ok: bool
    problems: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": [p[:300] for p in self.problems[:20]]}


def parse_claim(data: Mapping[str, Any]) -> Claim | None:
    job = data.get("job")
    if not job:
        return None
    brief = dict(data.get("brief") or {})
    fix_raw = data.get("fix")
    fix = None
    if fix_raw:
        fix = Fix(
            text=str(fix_raw.get("text") or ""),
            remember=bool(fix_raw.get("remember")),
            agent_session_id=fix_raw.get("agentSessionId") or None,
            version=int(fix_raw.get("version") or brief.get("version") or 1),
        )
    downloads = tuple(
        Download(path=str(d["path"]), url=str(d["url"]), size=d.get("size"))
        for d in data.get("downloads") or []
    )
    return Claim(
        kind=str(data.get("kind") or "edit"),
        job_id=str(job["id"]),
        show_date=str(job.get("showDate") or brief.get("showDate")),
        version=int(brief.get("version") or job.get("version") or 1),
        brief=brief,
        downloads=downloads,
        fix=fix,
    )


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class PortalClient:
    def __init__(self, base_url: str, token: "str | None | Callable[[], str | None]", *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Sleep = _default_sleep, timeout: float = 30.0):
        self._base = base_url.rstrip("/") + "/api/service/show-editor/"
        # A callable is read on every request, so a fresh link takes effect without a restart.
        self._token = token if callable(token) else (lambda: token)
        self._transport = transport
        self._sleep = sleep
        self._timeout = timeout

    async def _post(self, route: str, body: Mapping[str, Any]) -> Any:
        token = self._token()
        if not token:
            raise AuthError("This Mac isn't linked to the Portal")
        headers = {"Authorization": f"Bearer {token}"}
        last: Exception | None = None
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as client:
            for attempt in range(RETRIES + 1):
                if attempt:
                    await self._sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
                try:
                    res = await client.post(self._base + route, json=dict(body), headers=headers)
                except httpx.TransportError as e:
                    last = PortalError(f"Portal unreachable: {e.__class__.__name__}")
                    continue
                if res.status_code == 401:
                    raise AuthError("The Portal rejected the editor token (401)")
                if res.status_code >= 500:
                    last = PortalError(_message(res), res.status_code)
                    continue
                if res.status_code >= 400:
                    raise PortalError(_message(res), res.status_code)
                return (res.json() or {}).get("data")
        assert last is not None
        raise last

    async def heartbeat(self, *, version: str, busy: bool, health: Health, public_key: str | None = None,
                        backends: Sequence[Mapping[str, Any]] = (),
                        acks: Sequence[Mapping[str, Any]] = (),
                        command_acks: Sequence[Mapping[str, Any]] = (),
                        machine: Mapping[str, str] | None = None) -> HeartbeatReply:
        body: dict[str, Any] = {"version": version[:40], "busy": busy, "health": health.as_json(),
                                "backends": list(backends), "credentialAcks": list(acks)[:10],
                                "commandAcks": list(command_acks)[:10]}
        if public_key:
            body["publicKey"] = public_key
        if machine:
            body["machine"] = {"name": str(machine.get("name") or "")[:80], "kind": machine.get("kind", "dedicated")}
        data = await self._post("heartbeat", body) or {}
        settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
        target = settings.get("targetVersion") or data.get("targetVersion")
        order = settings.get("agentOrder")
        return HeartbeatReply(
            cancel_job_ids=tuple(str(i) for i in data.get("cancelJobIds") or []),
            target_version=(str(target).strip() or None) if target else None,
            credentials=tuple(c for c in data.get("credentials") or [] if isinstance(c, dict)),
            agent_order=tuple(str(a) for a in order) if isinstance(order, list) and order else None,
            commands=tuple(Command(str(c["id"]), str(c["kind"])) for c in data.get("commands") or []
                           if isinstance(c, dict) and c.get("id")
                           and (c.get("kind") in COMMAND_KINDS or str(c.get("kind")).startswith("shell:"))),
            active=data.get("active") is not False,
        )

    async def claim(self) -> Claim | None:
        return parse_claim(await self._post("claim", {}) or {})

    async def progress(self, job_id: str, stage: str, message: str) -> bool:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        data = await self._post(f"jobs/{job_id}/progress", {"stage": stage, "message": message[:300]})
        return bool((data or {}).get("cancel"))

    async def upload_targets(self, job_id: str) -> UploadTargets:
        data = await self._post(f"jobs/{job_id}/upload-targets", {}) or {}
        return UploadTargets(
            drive_base_url=str(data["driveBaseUrl"]).rstrip("/"),
            video=UploadTarget(path=str(data["video"]["path"]), token=str(data["video"]["token"])),
            poster=UploadTarget(path=str(data["poster"]["path"]), token=str(data["poster"]["token"])),
            log=_optional_target(data.get("log")),
        )

    async def complete(self, job_id: str, result: Mapping[str, Any]) -> None:
        await self._post(f"jobs/{job_id}/complete", result)

    async def fail(self, job_id: str, error: str, cancelled: bool = False, debug_log_path: str | None = None) -> None:
        body: dict[str, Any] = {"error": error[:1000], "cancelled": cancelled}
        if debug_log_path:
            body["debugLogPath"] = debug_log_path
        await self._post(f"jobs/{job_id}/fail", body)


def _optional_target(raw: Any) -> UploadTarget | None:
    if isinstance(raw, dict) and raw.get("path") and raw.get("token"):
        return UploadTarget(path=str(raw["path"]), token=str(raw["token"]))
    return None


def _message(res: httpx.Response) -> str:
    try:
        err = (res.json() or {}).get("error") or {}
        if err.get("message"):
            return f"Portal {res.status_code}: {err['message']}"
    except ValueError:
        pass
    return f"Portal {res.status_code}"
