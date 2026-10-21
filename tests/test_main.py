import asyncio

from editor import main as main_mod
from editor.main import EXIT_AUTH, LoopSettings, Worker
from editor.portal import AuthError, Claim, Command, HeartbeatReply, Health
from editor.service import EXIT_RESTART
from editor.worker import STANDBY, UNLINKED, RestartRequested
from editor.updater import EXIT_UPDATED, RestartForUpdate, UpdateResult

FAST = LoopSettings(heartbeat_s=0.01, poll_s=0.01, doctor_s=1000, housekeeping_s=1000)


class FakePortal:
    def __init__(self, claims=(), cancel_ids=(), target=None, credentials=(), commands=(), agent_order=None,
                 active=True, unauthorized=False):
        self.commands = list(commands)
        self.agent_order = agent_order
        self.active = active
        self.unauthorized = unauthorized
        self.target = target
        self.credentials = list(credentials)
        self.bodies = []
        self.healths = []
        self.claims = list(claims)
        self.heartbeats = []
        self.claim_calls = 0
        self.cancel_ids = list(cancel_ids)

    async def heartbeat(self, *, version, busy, health, public_key=None, backends=(), acks=(), command_acks=(),
                        machine=None):
        self.heartbeats.append(busy)
        self.healths.append(health)
        self.bodies.append({"publicKey": public_key, "backends": list(backends), "acks": list(acks),
                            "commandAcks": list(command_acks), "machine": machine})
        if self.unauthorized:
            raise AuthError("401")
        creds, self.credentials = tuple(self.credentials), []
        commands, self.commands = tuple(self.commands), []
        return HeartbeatReply(tuple(self.cancel_ids), self.target, creds, self.agent_order, commands, self.active)

    async def claim(self):
        if self.unauthorized:
            raise AuthError("401")
        self.claim_calls += 1
        return self.claims.pop(0) if self.claims else None


def claim(job_id="j1"):
    return Claim("edit", job_id, "2026-10-21", 1, {})


def worker(portal, run_job, health=Health(True), update=None, **kw):
    return Worker(portal=portal, version="0.1.0", doctor=lambda: health, run_job=run_job, settings=FAST,
                  update=update, **kw)


async def test_heartbeat_continues_during_a_job():
    portal = FakePortal([claim()])

    async def slow_job(c, cancel):
        await asyncio.sleep(0.2)
        return "REVIEW"

    w = worker(portal, slow_job)
    serve = asyncio.create_task(w.serve())
    await asyncio.sleep(0.3)
    serve.cancel()
    busy_beats = [b for b in portal.heartbeats if b]
    assert len(busy_beats) >= 5


async def test_worker_does_not_claim_while_unhealthy():
    portal = FakePortal([claim()])
    w = worker(portal, lambda c, e: None, health=Health(False, ("The MacBook is on battery; plug it in.",)))
    assert await w.step() is False
    assert portal.claim_calls == 0


async def test_heartbeat_cancel_sets_the_running_jobs_event():
    portal = FakePortal([claim("j9")], cancel_ids=["j9"])
    seen = {}

    async def job(c, cancel):
        for _ in range(100):
            if cancel.is_set():
                seen["cancelled"] = True
                return "CANCELLED"
            await asyncio.sleep(0.01)

    w = worker(portal, job)
    beat = asyncio.create_task(w.heartbeat_forever())
    await w.step()
    beat.cancel()
    assert seen == {"cancelled": True}


async def test_update_runs_before_claiming_and_restarts_on_success():
    portal = FakePortal([claim()])
    calls = []

    def update(target, problems):
        calls.append(target)
        return UpdateResult("updated", "v0.2.0", "Updated to v0.2.0.")

    w = worker(portal, lambda c, e: None, update=update)
    try:
        await w.step()
    except RestartForUpdate:
        pass
    else:
        raise AssertionError("expected a restart")
    assert calls == [None] and portal.claim_calls == 0


async def test_never_updates_mid_job():
    portal = FakePortal([claim()])
    calls = []

    def update(target, problems):
        calls.append(target)
        return UpdateResult("current")

    async def job(c, cancel):
        await w.maybe_update()  # as if a timer fired during the job
        return "REVIEW"

    w = worker(portal, job, update=update)
    w._update_at = 0.0  # pretend the start-up check just ran
    w._s = LoopSettings(heartbeat_s=0.01, poll_s=0.01, doctor_s=1000, housekeeping_s=1000, update_s=0)
    await w.step()
    assert calls == [None]  # the check before the claim ran; the one during the job didn't


async def test_pinned_target_from_the_heartbeat_and_failed_update_reported_in_health():
    portal = FakePortal(target="v0.1.5")
    seen = []

    def update(target, problems):
        seen.append(target)
        return UpdateResult("rolled_back", target, "Update to v0.1.5 failed: setup failed. Rolled back to v0.1.4.")

    w = worker(portal, lambda c, e: None, update=update)
    await w.heartbeat_once()
    await w.step()
    await w.heartbeat_once()
    assert seen == ["v0.1.5"]
    last = portal.healths[-1]
    assert last.ok and last.problems[-1].startswith("Update to v0.1.5 failed")


def test_restart_for_update_exits_with_the_update_code(monkeypatch):
    class Updated:
        async def serve(self):
            raise RestartForUpdate("Updated to v0.2.0.")

    monkeypatch.setattr(main_mod, "build_worker", lambda: Updated())
    assert main_mod.cli(["run"]) == EXIT_UPDATED == 10


async def test_heartbeat_installs_sealed_credentials_and_acks_next_time():
    from editor.backends import BackendStatus
    from editor.pairing import Ack

    class FakePairing:
        public_key = "cHVibGlj"
        installed = []

        def install_all(self, creds):
            self.installed.extend(creds)
            return [Ack(c.id, True) for c in creds]

    pairing = FakePairing()
    portal = FakePortal(credentials=[{"id": "c1", "backend": "codex", "ciphertext": "x"}])
    w = Worker(portal=portal, version="v1", doctor=lambda: Health(True), run_job=lambda c, e: None,
               settings=FAST, pairing=pairing,
               backend_statuses=lambda: [BackendStatus("codex", True, "Signed in with a ChatGPT account")])
    await w.heartbeat_once()
    await w.heartbeat_once()
    assert [c.id for c in pairing.installed] == ["c1"]
    assert portal.bodies[0]["publicKey"] == "cHVibGlj" and portal.bodies[0]["acks"] == []
    assert portal.bodies[0]["backends"] == [{"name": "codex", "connected": True,
                                             "detail": "Signed in with a ChatGPT account"}]
    assert portal.bodies[1]["acks"] == [{"id": "c1", "ok": True}]


def test_cli_connect_needs_no_config(monkeypatch):
    seen = {}

    def fake_connect(backend, code, portal, device_auth=False):
        seen.update(backend=backend, code=code, portal=portal, device=device_auth)

    monkeypatch.setattr("editor.connect.connect", fake_connect)
    monkeypatch.setenv("INFOCUS_EDITOR_HOME", "/nonexistent")
    assert main_mod.cli(["connect", "grok", "--code", "ABCD-1234", "--portal", "https://p.example.org",
                         "--device-auth"]) == 0
    assert seen == {"backend": "grok", "code": "ABCD-1234", "portal": "https://p.example.org", "device": True}


# ---- unlinked (401) ---------------------------------------------------------


async def test_401_marks_the_mac_unlinked_stops_claiming_and_keeps_running():
    portal = FakePortal([claim()], unauthorized=True)
    w = worker(portal, lambda c, e: None)
    w._s = LoopSettings(heartbeat_s=0.01, unlinked_heartbeat_s=0.05, poll_s=0.01, doctor_s=1000, housekeeping_s=1000)
    serve = asyncio.create_task(w.serve())
    await asyncio.sleep(0.2)
    assert not serve.done()  # no exit loop under launchd
    serve.cancel()
    assert w.unlinked and portal.claim_calls == 0
    assert 2 <= len(portal.heartbeats) <= 6  # retried on the slow (unlinked) interval, not every 0.01 s
    assert w.reported_health().problems[0] == UNLINKED
    assert UNLINKED == ("This Mac isn't linked to the Portal — open InFocus Editor Setup and choose Link again.")


async def test_linking_again_resumes_claiming():
    portal = FakePortal([claim()], unauthorized=True)
    ran = []
    w = worker(portal, lambda c, e: ran.append(c.job_id) or asyncio.sleep(0))
    await w.heartbeat_once()
    assert await w.step() is False
    portal.unauthorized = False
    await w.heartbeat_once()
    assert not w.unlinked and await w.step() is True and ran == ["j1"]


# ---- commands ------------------------------------------------------------------


class FakeCommands:
    def __init__(self, repair_ok=True):
        self.repaired = 0
        self.repair_ok = repair_ok

    def logs(self):
        return "2026-10-21 INFO editor: job j1 finished: REVIEW"

    def repair(self):
        self.repaired += 1
        return self.repair_ok, "==> Video tools ready\n@@done"


async def test_doctor_and_logs_run_even_mid_job_and_are_acked_next_heartbeat():
    portal = FakePortal(commands=[Command("k1", "doctor"), Command("k2", "logs")], active=False)
    w = worker(portal, lambda c, e: None, health=Health(True, ("Warning: no videotoolbox",)), commands=FakeCommands())
    w.busy = True
    await w.heartbeat_once()
    await w.heartbeat_once()
    acks = portal.bodies[1]["commandAcks"]
    assert [a["id"] for a in acks] == ["k1", "k2"] and all(a["ok"] for a in acks)
    assert STANDBY in acks[0]["output"] and "Healthy" in acks[0]["output"]
    assert "job j1 finished" in acks[1]["output"]


async def test_update_restart_and_repair_wait_until_idle():
    portal = FakePortal(commands=[Command("k3", "repair")])
    commands = FakeCommands()
    w = worker(portal, lambda c, e: None, commands=commands)
    w.busy = True
    await w.heartbeat_once()
    await w.run_queued_commands()
    assert commands.repaired == 0  # mid-job: queued
    w.busy = False
    try:
        await w.run_queued_commands()
    except RestartRequested:
        pass
    else:
        raise AssertionError("a finished repair restarts the editor")
    assert commands.repaired == 1
    sent = [a for body in portal.bodies for a in body["commandAcks"]]
    assert sent == [{"id": "k3", "ok": True, "output": "==> Video tools ready\n@@done"}]


async def test_restart_command_acks_then_exits_for_launchd():
    portal = FakePortal(commands=[Command("k4", "restart")])
    exits = []
    w = worker(portal, lambda c, e: None, before_exit=lambda: exits.append(1))
    await w.heartbeat_once()
    try:
        await w.step()
    except RestartRequested:
        pass
    else:
        raise AssertionError("expected a restart")
    assert portal.bodies[-1]["commandAcks"] == [{"id": "k4", "ok": True, "output": "Restarting."}]
    assert exits == [1] and portal.claim_calls == 0


async def test_update_command_checks_now():
    portal = FakePortal(commands=[Command("k5", "update")])
    calls = []

    def update(target, problems):
        calls.append(target)
        return UpdateResult("current", "v0.1.0")

    w = worker(portal, lambda c, e: None, update=update)
    w._update_at = 0.0
    w._s = LoopSettings(heartbeat_s=0.01, poll_s=0.01, doctor_s=1000, housekeeping_s=1000, update_s=10_000)
    await w.heartbeat_once()
    await w.step()
    await w.heartbeat_once()
    assert calls == [None]
    assert portal.bodies[-1]["commandAcks"][0]["id"] == "k5" and portal.bodies[-1]["commandAcks"][0]["ok"]


def test_restart_exit_code(monkeypatch):
    class Restarting:
        async def serve(self):
            raise RestartRequested("restart requested from the Portal")

    monkeypatch.setattr(main_mod, "build_worker", lambda: Restarting())
    assert main_mod.cli(["run"]) == EXIT_RESTART == 11


# ---- settings and machines --------------------------------------------------------


async def test_portal_agent_order_overrides_config():
    orders = []
    portal = FakePortal(agent_order=("grok", "claude"))
    w = worker(portal, lambda c, e: None, set_agent_order=orders.append)
    await w.heartbeat_once()
    assert orders == [("grok", "claude")]


def test_pool_set_order_replaces_the_config_order():
    from editor.backends import BackendPool, BackendStatus

    class B:
        def __init__(self, name):
            self.name, self.label = name, name.title()

        def status(self):
            return BackendStatus(self.name, True, "ok")

    pool = BackendPool([B("claude"), B("codex"), B("grok")], ["claude"])
    assert pool.names == ["claude"]
    pool.set_order(["grok", "codex", "nonsense"])
    assert pool.names == ["grok", "codex"]


async def test_machine_info_is_sent_and_inactive_machines_still_ask_for_work():
    portal = FakePortal([claim()], active=False)
    w = worker(portal, lambda c, e: asyncio.sleep(0), machine={"name": "Studio MacBook Pro", "kind": "dedicated"})
    await w.heartbeat_once()
    assert portal.bodies[0]["machine"] == {"name": "Studio MacBook Pro", "kind": "dedicated"}
    assert w.active is False and w.status_lines() == (STANDBY,)
    assert await w.step() is True and portal.claim_calls == 1  # the Portal decides what standby gets


def test_logs_are_tailed_and_scrubbed(tmp_path):
    from editor.logs import tail_log

    log = tmp_path / "infocus-editor.log"
    lines = [f"line {i}\n" for i in range(150)] + [
        "auth Authorization: Bearer abc.def.ghi\n",
        "token sk-ant-oat01-" + "z" * 60 + "\n",
        'json {"refresh_token": "rt-secret-value", "ok": true}\n',
        "url https://drive.example.org/api/service/file?path=x&token=rw.1777777777.deadbeef\n",
    ]
    log.write_text("".join(lines))
    out = tail_log(log)
    assert "line 49\n" not in out and "line 149" in out
    for secret in ("abc.def.ghi", "z" * 20, "rt-secret-value", "deadbeef"):
        assert secret not in out, secret
    assert len(out.splitlines()) == 100
