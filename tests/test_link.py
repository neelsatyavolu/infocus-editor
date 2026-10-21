import json

import httpx
import pytest

from editor.keychain import MemoryKeychain
from editor.link import LinkError, link_works, run_link

PORTAL = "https://portal.example.org"


class FakePortal:
    """Scripted /link and /link/poll replies."""

    def __init__(self, polls_per_link):
        self.polls_per_link = [list(p) for p in polls_per_link]
        self.starts = 0
        self.polled = []

    def __call__(self, req: httpx.Request):
        path = req.url.path
        assert "authorization" not in req.headers
        body = json.loads(req.content)
        if path.endswith("/show-editor/link"):
            self.starts += 1
            assert body == {"deviceName": "Studio Mac"}
            n = self.starts
            return httpx.Response(200, json={"data": {
                "deviceCode": f"dev-{n}", "userCode": f"K7Q{n}-M9XD",
                "verificationUrl": f"{PORTAL}/show-roles?link=K7Q{n}-M9XD",
                "expiresAt": "2026-10-21T12:15:00Z", "interval": 5}})
        if path.endswith("/link/poll"):
            self.polled.append(body["deviceCode"])
            queue = self.polls_per_link[self.starts - 1]
            reply = queue.pop(0)
            return httpx.Response(200, json={"data": reply})
        return httpx.Response(404)


def link(portal, keychain, progress=True):
    lines, sleeps = [], []
    run_link(PORTAL, keychain, progress=progress, transport=httpx.MockTransport(portal),
             sleep=sleeps.append, out=lines.append, name="Studio Mac")
    return lines, sleeps


def test_pending_then_approved_saves_the_token():
    portal = FakePortal([[{"status": "pending"}, {"status": "pending"}, {"status": "approved", "token": "tok-abc"}]])
    keychain = MemoryKeychain()
    lines, sleeps = link(portal, keychain)
    assert lines == [f"@@link K7Q1-M9XD {PORTAL}/show-roles?link=K7Q1-M9XD", "@@linked"]
    assert keychain.get("editor_token") == "tok-abc"
    assert sleeps == [5.0, 5.0, 5.0] and portal.polled == ["dev-1"] * 3


def test_expired_code_starts_a_new_link_with_a_new_code():
    portal = FakePortal([[{"status": "pending"}, {"status": "expired"}],
                         [{"status": "approved", "token": "tok-2"}]])
    keychain = MemoryKeychain()
    lines, _ = link(portal, keychain)
    assert lines[0].startswith("@@link K7Q1-M9XD") and lines[1].startswith("@@link K7Q2-M9XD")
    assert lines[-1] == "@@linked" and keychain.get("editor_token") == "tok-2"
    assert portal.polled == ["dev-1", "dev-1", "dev-2"]


def test_denied_is_a_plain_error():
    portal = FakePortal([[{"status": "denied"}]])
    keychain = MemoryKeychain()
    with pytest.raises(LinkError, match="declined"):
        link(portal, keychain)
    assert keychain.get("editor_token") is None


def test_human_output_without_progress():
    portal = FakePortal([[{"status": "approved", "token": "t"}]])
    lines, _ = link(portal, MemoryKeychain(), progress=False)
    assert "approve this Mac" in lines[0] and "K7Q1-M9XD" in lines[0] and lines[1] == "Linked to the Portal."


def test_link_works_checks_the_saved_token_with_a_heartbeat():
    def handler(req):
        assert req.url.path.endswith("/heartbeat")
        return httpx.Response(200 if req.headers["authorization"] == "Bearer good" else 401,
                              json={"data": {}})

    transport = httpx.MockTransport(handler)
    assert link_works(PORTAL, "good", transport) is True
    assert link_works(PORTAL, "revoked", transport) is False
    assert link_works(PORTAL, None, transport) is False
