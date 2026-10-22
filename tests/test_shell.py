"""editor/shell.py: only a command signed with the admin key, for this command id and this machine, before it
expires, runs, and only on a dedicated Mac."""
import asyncio
import base64
import hashlib
import json

import pytest
from nacl.signing import SigningKey

from editor import shell
from editor.portal import COMMAND_KINDS

ADMIN = SigningKey.generate()
PUBLIC = base64.b64encode(bytes(ADMIN.verify_key)).decode()
TOKEN = "device-token-1"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()
NOW = 1_800_000_000.0


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def signed(cmd="echo hi", *, cid="c1", device=TOKEN_HASH, exp=NOW + 600, key=ADMIN, **extra):
    body = b64url(json.dumps({"v": 1, "id": cid, "device": device, "exp": exp, "cmd": cmd, **extra}).encode())
    return f"shell:{body}.{b64url(key.sign(body.encode()).signature)}"


def remote(kind="dedicated", token=TOKEN):
    return shell.RemoteShell(kind, lambda: token, public_key=lambda: PUBLIC, clock=lambda: NOW)


async def test_a_signed_command_runs_and_returns_its_output():
    ok, out = await remote()("c1", signed("echo hello from the editor"))
    assert ok and "hello from the editor" in out and "[exit 0]" in out


@pytest.mark.parametrize("kind, why", [
    (signed(cid="c2"), "another command"),
    (signed(device="0" * 64), "another machine"),
    (signed(exp=NOW - 1), "expired"),
    (signed(exp=NOW + 7200), "expired"),
    (signed(key=SigningKey.generate()), "bad signature"),
    (signed(cmd="  "), "no command"),
    ("shell:not-signed", "not a signed"),
])
async def test_refuses_anything_not_signed_for_this_command_machine_and_time(kind, why):
    ok, out = await remote()("c1", kind)
    assert not ok and out.startswith("Refused") and why in out


async def test_a_tampered_command_is_refused():
    _, sig = signed().removeprefix("shell:").split(".")
    evil = b64url(json.dumps({"v": 1, "id": "c1", "device": TOKEN_HASH, "exp": NOW + 600, "cmd": "id"}).encode())
    ok, out = await remote()("c1", f"shell:{evil}.{sig}")
    assert not ok and "bad signature" in out


async def test_a_personal_computer_refuses_every_remote_command():
    ok, out = await remote("personal")("c1", signed())
    assert not ok and "dedicated" in out


async def test_an_unlinked_mac_refuses():
    ok, out = await remote(token=None)("c1", signed())
    assert not ok and "linked" in out


async def test_a_command_over_its_time_limit_is_killed():
    ok, out = await shell.run(shell.ShellRequest("echo started; sleep 30", 1))
    assert not ok and "started" in out and "timed out after 1 s" in out


def test_timeouts_are_capped():
    request = shell.verify(signed(timeout=99_999), "c1", TOKEN_HASH, NOW, PUBLIC)
    assert request.timeout_s == shell.MAX_TIMEOUT_S


def test_the_shipped_admin_key_is_a_32_byte_ed25519_key():
    assert len(base64.b64decode(shell.PUBLIC_KEY_FILE.read_text().strip())) == 32


def test_the_power_commands_are_accepted_from_the_portal():
    assert {"always-on", "normal-power"} <= set(COMMAND_KINDS)


async def test_the_worker_runs_shell_commands_in_the_background_and_acks_them():
    from editor.portal import Command, Health
    from tests.test_main import FakePortal, worker

    portal = FakePortal(commands=[Command("s1", signed(cid="s1"))], active=False)
    calls = []

    async def fake_shell(cid, kind):
        calls.append(cid)
        return True, "done"

    w = worker(portal, lambda c, e: None, health=Health(True, ()), shell=fake_shell)
    await w.heartbeat_once()
    await asyncio.sleep(0)
    await w.heartbeat_once()
    assert calls == ["s1"]
    assert portal.bodies[1]["commandAcks"] == [{"id": "s1", "ok": True, "output": "done"}]
