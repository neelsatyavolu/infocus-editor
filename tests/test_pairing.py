import json
import stat
import subprocess

import httpx
import pytest

from editor.backends import BackendPool, BackendStatus
from editor.connect import ConnectError, TokenMasker, capture_pty, connect, signin_claude, signin_home
from editor.keychain import MemoryKeychain
from editor.pairing import KEY_ITEM, Credential, Pairing, parse_credentials, seal
from tests.helpers import make_config

TOKEN = "sk-ant-oat01-" + "Ab_9-" * 20
CODEX_AUTH = json.dumps({"OPENAI_API_KEY": None, "tokens": {"access_token": "at", "refresh_token": "rt"}})


class FakeBackend:
    def __init__(self, name, problem=None):
        self.name, self.label, self.problem = name, name.title(), problem
        self.verified = 0

    def status(self):
        return BackendStatus(self.name, True, "ok")

    def verify(self):
        self.verified += 1
        return self.problem


def pairing(tmp_path, problem=None):
    cfg = make_config(tmp_path)
    keychain = MemoryKeychain()
    backends = [FakeBackend("claude", problem), FakeBackend("codex", problem), FakeBackend("grok", problem)]
    pool = BackendPool(backends, cfg.agents)
    return Pairing(keychain, pool, cfg.codex_home, cfg.grok_home), keychain, cfg, backends


def test_key_pair_is_created_once_and_kept_in_the_keychain(tmp_path):
    p, keychain, cfg, _ = pairing(tmp_path)
    assert keychain.get(KEY_ITEM)
    again = Pairing(keychain, BackendPool([], cfg.agents), cfg.codex_home, cfg.grok_home)
    assert again.public_key == p.public_key


def test_seal_unseal_round_trip(tmp_path):
    p, *_ = pairing(tmp_path)
    payload = {"backend": "claude", "kind": "oauth_token", "value": TOKEN}
    assert p.unseal(seal(p.public_key, payload)) == payload


def test_installs_a_claude_token_in_the_keychain_and_acks(tmp_path):
    p, keychain, _, backends = pairing(tmp_path)
    ct = seal(p.public_key, {"backend": "claude", "kind": "oauth_token", "value": TOKEN})
    acks = p.install_all([Credential("c1", "claude", ct)])
    assert [a.as_json() for a in acks] == [{"id": "c1", "ok": True}]
    assert keychain.get("claude_oauth_token") == TOKEN and backends[0].verified == 1


@pytest.mark.parametrize("backend", ["codex", "grok"])
def test_installs_auth_json_with_owner_only_modes(tmp_path, backend):
    p, _, cfg, _ = pairing(tmp_path)
    ct = seal(p.public_key, {"backend": backend, "kind": "auth_json", "value": CODEX_AUTH})
    ack = p.install(Credential("c2", backend, ct))
    home = cfg.codex_home if backend == "codex" else cfg.grok_home
    auth = home / "auth.json"
    assert ack.ok and auth.read_text() == CODEX_AUTH
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600 and stat.S_IMODE(home.stat().st_mode) == 0o700


def test_failed_verification_is_acked_without_the_secret(tmp_path):
    p, *_ = pairing(tmp_path, problem="Codex says it isn't signed in; connect Codex again.")
    ct = seal(p.public_key, {"backend": "codex", "kind": "auth_json", "value": CODEX_AUTH})
    ack = p.install(Credential("c3", "codex", ct))
    assert not ack.ok and "connect Codex again" in ack.error and "rt" not in ack.error


def test_rejects_wrong_key_wrong_backend_and_bad_tokens(tmp_path):
    p, keychain, *_ = pairing(tmp_path)
    other, *_ = pairing(tmp_path / "other")
    bad_key = p.install(Credential("a", "claude", seal(other.public_key, {"backend": "claude"})))
    assert not bad_key.ok and "different Mac key" in bad_key.error
    mismatch = p.install(Credential("b", "codex", seal(p.public_key, {"backend": "grok", "kind": "auth_json",
                                                                      "value": "{}"})))
    assert not mismatch.ok
    api_key = p.install(Credential("c", "claude", seal(p.public_key, {"backend": "claude", "kind": "oauth_token",
                                                                      "value": "sk-ant-api03-abc"})))
    assert not api_key.ok and keychain.get("claude_oauth_token") is None


def test_parse_credentials_skips_malformed_items():
    assert parse_credentials([{"id": "1", "backend": "codex", "ciphertext": "x"}, {"id": "2"}]) == (
        Credential("1", "codex", "x"),)


# ---- connect (on the producer's computer) ---------------------------------


class FakePortal:
    def __init__(self, public_key, backend="codex", status=200):
        self.public_key, self.backend, self.status = public_key, backend, status
        self.posted = None

    def __call__(self, req: httpx.Request):
        assert req.url.path == "/api/service/show-editor/pair/ABCD-1234"
        assert "authorization" not in req.headers
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "nope"}})
        if req.method == "GET":
            return httpx.Response(200, json={"data": {"backend": self.backend, "publicKey": self.public_key,
                                                      "expiresAt": "2026-10-21T12:00:00Z"}})
        self.posted = json.loads(req.content)
        return httpx.Response(200, json={"data": {"ok": True}})


def test_connect_seals_the_sign_in_so_only_the_mac_can_read_it(tmp_path, capsys):
    mac, *_ = pairing(tmp_path)
    portal = FakePortal(mac.public_key)

    def fake_signin(backend, device):
        assert backend == "codex" and device is True
        return {"backend": "codex", "kind": "auth_json", "value": CODEX_AUTH}

    connect("codex", "abcd-1234", "https://portal.example.org/", device_auth=True,
            transport=httpx.MockTransport(portal), signin=fake_signin)
    ct = portal.posted["ciphertext"]
    assert "refresh_token" not in ct
    assert mac.unseal(ct)["value"] == CODEX_AUTH
    out = capsys.readouterr().out
    assert "Connected Codex" in out and "rt" not in out.replace("Portal", "")


def test_connect_errors_are_plain(tmp_path):
    mac, *_ = pairing(tmp_path)
    with pytest.raises(ConnectError, match="unknown, expired or already used"):
        connect("codex", "ABCD-1234", "https://p.example.org", transport=httpx.MockTransport(
            FakePortal(mac.public_key, status=404)), signin=lambda b, d: {})
    with pytest.raises(ConnectError, match="for grok, not codex"):
        connect("codex", "ABCD-1234", "https://p.example.org", transport=httpx.MockTransport(
            FakePortal(mac.public_key, backend="grok")), signin=lambda b, d: {})
    with pytest.raises(ConnectError, match="https"):
        connect("codex", "ABCD-1234", "http://p.example.org", signin=lambda b, d: {})
    with pytest.raises(ConnectError, match="XXXX-XXXX"):
        connect("codex", "nope", "https://p.example.org", signin=lambda b, d: {})


def test_codex_sign_in_uses_a_throwaway_home_and_wipes_it(tmp_path):
    seen = {}

    def fake_login(cmd, env, check):
        seen["cmd"], seen["home"] = cmd, env["CODEX_HOME"]
        from pathlib import Path
        (Path(env["CODEX_HOME"]) / "auth.json").write_text(CODEX_AUTH)
        return subprocess.CompletedProcess(cmd, 0)

    payload = signin_home("codex", True, run=fake_login)
    assert payload == {"backend": "codex", "kind": "auth_json", "value": CODEX_AUTH}
    assert seen["cmd"] == ["codex", "login", "--device-auth"]
    from pathlib import Path
    assert not Path(seen["home"]).exists() and ".codex" not in seen["home"]


def test_grok_sign_in_command(tmp_path):
    seen = {}

    def fake_login(cmd, env, check):
        seen["cmd"] = cmd
        from pathlib import Path
        (Path(env["GROK_HOME"]) / "auth.json").write_text('{"access_token": "x"}')
        return subprocess.CompletedProcess(cmd, 0)

    signin_home("grok", False, run=fake_login)
    assert seen["cmd"] == ["grok", "login", "--oauth"]


def test_claude_sign_in_uses_the_captured_token(monkeypatch):
    monkeypatch.setattr("editor.connect.claude_cli", lambda: "/fake/claude")
    calls = []
    payload = signin_claude(False, capture=lambda cmd: (calls.append(cmd), (0, TOKEN))[1])
    assert calls == [["/fake/claude", "setup-token"]] and payload["value"] == TOKEN
    with pytest.raises(ConnectError):
        signin_claude(False, capture=lambda cmd: (1, None))


def test_token_masker_hides_the_token_even_when_split():
    m = TokenMasker()
    shown = m.feed("Your token: sk-ant-o") + m.feed("at01-" + "x" * 30) + m.feed("y" * 30 + "\nDone\n") + m.flush()
    assert "x" * 5 not in shown and "Done" in shown and "captured, not shown" in shown
    assert m.token == "sk-ant-oat01-" + "x" * 30 + "y" * 30


def test_capture_pty_runs_a_real_program_and_masks(tmp_path):
    out = []
    code, token = capture_pty(["/bin/sh", "-c", f"echo 'Visit the link'; echo '{TOKEN}'; echo bye"],
                              write=out.append)
    shown = "".join(out)
    assert code == 0 and token == TOKEN
    assert TOKEN not in shown and "Visit the link" in shown and "bye" in shown
