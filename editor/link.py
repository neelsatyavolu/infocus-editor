"""`infocus-editor link [--progress] [--check]`: link this Mac to the Portal with a
device code (docs/portal-api.md, "Linking the Mac"). Nobody copies a token.

1. POST /link with the Mac's name → {deviceCode, userCode, verificationUrl, expiresAt, interval}
2. Show the code (`@@link <userCode> <url>` with --progress); a producer approves it in the Portal.
3. POST /link/poll every `interval` s → pending | expired | denied | approved (+ token, once).
4. Save the token in the Keychain (`editor_token`) and print `@@linked`.
An expired code starts a new link with a new code.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

import httpx

TOKEN_ITEM = "editor_token"
MAX_STARTS = 8  # 8 × 15 min of waiting for someone to approve
MAX_NETWORK_FAILURES = 30

Out = Callable[[str], None]


class LinkError(Exception):
    """A plain reason, shown in the setup app."""


@dataclass(frozen=True)
class LinkStart:
    device_code: str
    user_code: str
    url: str
    interval: float


def device_name() -> str:
    try:
        name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True,
                              check=False, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        name = ""
    return (name or socket.gethostname() or "Editing Mac")[:80]


def _service(portal_url: str) -> str:
    return portal_url.rstrip("/") + "/api/service/show-editor/"


def _data(res: httpx.Response) -> dict:
    if res.status_code >= 400:
        try:
            message = (res.json().get("error") or {}).get("message")
        except ValueError:
            message = None
        raise LinkError(f"The Portal answered {res.status_code}" + (f": {message}" if message else "."))
    return (res.json() or {}).get("data") or {}


def start_link(http: httpx.Client, portal_url: str, name: str) -> LinkStart:
    data = _data(http.post(_service(portal_url) + "link", json={"deviceName": name}))
    try:
        return LinkStart(str(data["deviceCode"]), str(data["userCode"]), str(data["verificationUrl"]),
                         float(data.get("interval") or 5))
    except KeyError as e:
        raise LinkError("The Portal's link reply is missing fields.") from e


def poll_link(http: httpx.Client, portal_url: str, start: LinkStart, sleep: Callable[[float], None]) -> tuple[str, str | None]:
    """Wait until the code is approved, denied or expired. Returns (status, token)."""
    failures = 0
    while True:
        sleep(start.interval)
        try:
            res = http.post(_service(portal_url) + "link/poll", json={"deviceCode": start.device_code})
        except httpx.TransportError:
            failures += 1
            if failures >= MAX_NETWORK_FAILURES:
                raise LinkError("Lost the connection to the Portal while waiting for approval.")
            continue
        if res.status_code == 429:
            sleep(start.interval)
            continue
        if res.status_code >= 500:
            failures += 1
            if failures >= MAX_NETWORK_FAILURES:
                raise LinkError(f"The Portal kept answering {res.status_code} while waiting for approval.")
            continue
        failures = 0
        data = _data(res)
        status = str(data.get("status") or "")
        if status == "approved":
            token = str(data.get("token") or "")
            if not token:
                raise LinkError("The Portal approved the link but sent no token; choose Link again.")
            return status, token
        if status in ("expired", "denied"):
            return status, None


def run_link(portal_url: str, keychain, *, progress: bool = False, transport: httpx.BaseTransport | None = None,
             sleep: Callable[[float], None] = time.sleep, out: Out = print, name: str | None = None) -> None:
    name = name or device_name()
    with httpx.Client(transport=transport, timeout=30) as http:
        for _ in range(MAX_STARTS):
            start = start_link(http, portal_url, name)
            if progress:
                out(f"@@link {start.user_code} {start.url}")
            else:
                out(f"Open {start.url} and approve this Mac. Its code is {start.user_code}.")
            status, token = poll_link(http, portal_url, start, sleep)
            if status == "approved" and token:
                keychain.set(TOKEN_ITEM, token)
                out("@@linked" if progress else "Linked to the Portal.")
                return
            if status == "denied":
                raise LinkError("A producer declined linking this Mac. Choose Link again to get a new code.")
            # expired: a new code
    raise LinkError("Nobody approved the link in time. Choose Link again to get a new code.")


def link_status(portal_url: str, token: str | None, transport: httpx.BaseTransport | None = None,
                machine_kind: str = "dedicated") -> str:
    """"active" | "standby" | "unlinked" | "unreachable", from one heartbeat with the saved token."""
    if not token:
        return "unlinked"
    body = {"version": "setup", "busy": False, "health": {"ok": False, "problems": ["Checking the link"]},
            "backends": [], "credentialAcks": [], "commandAcks": [],
            "machine": {"name": device_name(), "kind": machine_kind}}
    try:
        with httpx.Client(transport=transport, timeout=20) as http:
            res = http.post(_service(portal_url) + "heartbeat", json=body,
                            headers={"Authorization": f"Bearer {token}"})
    except httpx.TransportError:
        return "unreachable"
    if res.status_code in (401, 403):
        return "unlinked"
    if res.status_code >= 400:
        return "unreachable"
    try:
        active = ((res.json() or {}).get("data") or {}).get("active")
    except ValueError:
        active = None
    return "standby" if active is False else "active"


def link_works(portal_url: str, token: str | None, transport: httpx.BaseTransport | None = None) -> bool:
    """True when the saved token is accepted (a heartbeat succeeds)."""
    return link_status(portal_url, token, transport) in ("active", "standby")


def cli_link(progress: bool, check: bool) -> int:
    from editor.config import Config, ConfigError
    from editor.keychain import Keychain

    try:
        cfg = Config.load()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1
    keychain = Keychain()
    if check:
        return 0 if link_works(cfg.portal_url, keychain.get(TOKEN_ITEM)) else 1
    try:
        run_link(cfg.portal_url, keychain, progress=progress,
                 out=lambda line: (print(line), sys.stdout.flush()))
    except (LinkError, httpx.HTTPError) as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0
