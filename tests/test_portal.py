import json

import httpx
import pytest

from editor.portal import AuthError, Health, PortalClient, PortalError

BASE = "https://portal.example.org"


async def no_sleep(_):
    return None


def client_with(handler):
    return PortalClient(BASE, "tok-123", transport=httpx.MockTransport(handler), sleep=no_sleep)


def ok(data):
    return httpx.Response(200, json={"data": data})


async def test_sends_the_bearer_token_and_route():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return ok({"cancelJobIds": ["j1"], "targetVersion": "v0.3.1"})

    reply = await client_with(handler).heartbeat(version="0.1.0", busy=True, health=Health(False, ("on battery",)))
    assert reply.cancel_job_ids == ("j1",) and reply.target_version == "v0.3.1"
    assert seen["auth"] == "Bearer tok-123"
    assert seen["url"] == f"{BASE}/api/service/show-editor/heartbeat"
    assert seen["body"] == {"version": "0.1.0", "busy": True, "health": {"ok": False, "problems": ["on battery"]},
                            "backends": [], "credentialAcks": [], "commandAcks": []}


async def test_claim_returns_none_on_null_job():
    assert await client_with(lambda req: ok({"job": None})).claim() is None


async def test_claim_parses_an_edit_and_a_fix():
    edit = {
        "kind": "edit",
        "job": {"id": "j1", "showDate": "2026-10-21", "version": 1},
        "brief": {"jobId": "j1", "showDate": "2026-10-21", "version": 1, "sourcePaths": ["Shows/a.mov"]},
        "downloads": [{"path": "Shows/a.mov", "url": "https://drive.example.org/f?token=x", "size": 10}],
    }
    claim = await client_with(lambda req: ok(edit)).claim()
    assert claim.kind == "edit" and claim.job_id == "j1" and claim.downloads[0].size == 10
    assert "token=x" not in repr(claim.downloads[0])

    fix = {**edit, "kind": "fix", "downloads": [], "brief": {**edit["brief"], "version": 2},
           "fix": {"text": "change the card", "remember": True, "agentSessionId": "s1", "version": 2}}
    claim = await client_with(lambda req: ok(fix)).claim()
    assert claim.kind == "fix" and claim.version == 2
    assert claim.fix.text == "change the card" and claim.fix.remember and claim.fix.agent_session_id == "s1"


async def test_progress_returns_the_cancel_flag():
    assert await client_with(lambda req: ok({"cancel": True})).progress("j1", "render", "Rendering") is True
    assert await client_with(lambda req: ok({"cancel": False})).progress("j1", "render", "x") is False


async def test_progress_rejects_unknown_stage():
    with pytest.raises(ValueError):
        await client_with(lambda req: ok({})).progress("j1", "dance", "x")


async def test_401_raises_auth_error_without_retry():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "nope"}})

    with pytest.raises(AuthError):
        await client_with(handler).claim()
    assert len(calls) == 1


async def test_5xx_is_retried_3_times_then_raises():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "busy"}})

    with pytest.raises(PortalError) as err:
        await client_with(handler).claim()
    assert len(calls) == 4  # first try + 3 retries
    assert "busy" in str(err.value)


async def test_network_error_recovers_on_retry():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("down")
        return ok({"job": None})

    assert await client_with(handler).claim() is None
    assert len(calls) == 3


async def test_4xx_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(409, json={"error": {"message": "Output path mismatch"}})

    with pytest.raises(PortalError) as err:
        await client_with(handler).complete("j1", {"outputNasPath": "x"})
    assert err.value.status == 409 and len(calls) == 1


async def test_upload_targets_and_fail():
    bodies = []

    def handler(req):
        bodies.append((req.url.path, json.loads(req.content)))
        if req.url.path.endswith("upload-targets"):
            return ok({"driveBaseUrl": "https://drive.example.org/",
                       "video": {"path": "Package Storage/Shows/d/v.mp4", "token": "rw.1.a"},
                       "poster": {"path": "Package Storage/Shows/d/v.poster.jpg", "token": "rw.1.b"}})
        return ok({"ok": True})

    c = client_with(handler)
    t = await c.upload_targets("j1")
    assert t.drive_base_url == "https://drive.example.org" and t.video.token == "rw.1.a"
    assert t.log is None  # an older Portal mints no debug log target
    await c.fail("j1", "x" * 2000, cancelled=True)
    path, body = bodies[-1]
    assert path.endswith("/jobs/j1/fail") and len(body["error"]) == 1000 and body["cancelled"] is True
    assert "debugLogPath" not in body


async def test_upload_targets_with_a_log_target_and_fail_with_its_path():
    bodies = []
    log_path = "Package Storage/Shows/d/editor-logs/j1-v1-111.tar.gz"

    def handler(req):
        bodies.append(json.loads(req.content))
        if req.url.path.endswith("upload-targets"):
            return ok({"driveBaseUrl": "https://drive.example.org",
                       "video": {"path": "v.mp4", "token": "rw.1.a"}, "poster": {"path": "v.jpg", "token": "rw.1.b"},
                       "log": {"path": log_path, "token": "rw.1.c"}})
        return ok({"ok": True})

    c = client_with(handler)
    t = await c.upload_targets("j1")
    assert t.log.path == log_path and t.log.token == "rw.1.c" and "rw.1.c" not in repr(t)
    await c.fail("j1", "boom", debug_log_path=log_path)
    assert bodies[-1] == {"error": "boom", "cancelled": False, "debugLogPath": log_path}


async def test_heartbeat_without_a_target_version():
    reply = await client_with(lambda req: ok({"cancelJobIds": [], "targetVersion": None})).heartbeat(
        version="v0.1.0", busy=False, health=Health(True))
    assert reply.target_version is None and reply.cancel_job_ids == ()


async def test_heartbeat_sends_key_backends_acks_and_reads_credentials():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return ok({"cancelJobIds": [], "targetVersion": None,
                   "credentials": [{"id": "c1", "backend": "codex", "ciphertext": "AAAA"}]})

    reply = await client_with(handler).heartbeat(
        version="v1", busy=False, health=Health(True), public_key="cHVi",
        backends=[{"name": "codex", "connected": False, "detail": "Not connected"}],
        acks=[{"id": "c0", "ok": True}])
    assert seen["body"]["publicKey"] == "cHVi"
    assert seen["body"]["backends"][0]["name"] == "codex" and seen["body"]["credentialAcks"] == [{"id": "c0", "ok": True}]
    assert reply.credentials == ({"id": "c1", "backend": "codex", "ciphertext": "AAAA"},)


async def test_heartbeat_reads_settings_and_commands():
    def handler(req):
        return ok({"cancelJobIds": [], "targetVersion": "v0.2.0",
                   "settings": {"agentOrder": ["codex", "claude"], "targetVersion": "v0.2.0"},
                   "commands": [{"id": "k1", "kind": "logs"}, {"id": "k2", "kind": "format-disk"}]})

    reply = await client_with(handler).heartbeat(version="v1", busy=False, health=Health(True),
                                                 command_acks=[{"id": "k0", "ok": True, "output": "Healthy"}])
    assert reply.agent_order == ("codex", "claude") and reply.target_version == "v0.2.0"
    assert [(c.id, c.kind) for c in reply.commands] == [("k1", "logs")]  # unknown kinds are ignored


async def test_unlinked_mac_raises_auth_error_without_a_request():
    calls = []
    c = PortalClient(BASE, None, transport=httpx.MockTransport(lambda r: calls.append(r) or ok({})), sleep=no_sleep)
    with pytest.raises(AuthError):
        await c.claim()
    assert calls == []


async def test_token_provider_is_read_on_every_request():
    tokens = iter(["old", "new"])
    seen = []

    def handler(req):
        seen.append(req.headers["authorization"])
        return ok({"job": None})

    c = PortalClient(BASE, lambda: next(tokens), transport=httpx.MockTransport(handler), sleep=no_sleep)
    await c.claim()
    await c.claim()
    assert seen == ["Bearer old", "Bearer new"]


async def test_heartbeat_sends_machine_info_and_reads_active():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return ok({"cancelJobIds": [], "active": False})

    reply = await client_with(handler).heartbeat(version="v1", busy=False, health=Health(True),
                                                 machine={"name": "Abby's MacBook Air", "kind": "personal"})
    assert seen["body"]["machine"] == {"name": "Abby's MacBook Air", "kind": "personal"}
    assert reply.active is False
    reply = await client_with(lambda r: ok({"cancelJobIds": []})).heartbeat(version="v1", busy=False,
                                                                            health=Health(True))
    assert reply.active is True  # older Portals don't send it
