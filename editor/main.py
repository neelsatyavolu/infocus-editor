"""Entry point: `infocus-editor run | doctor | link | connect`."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from editor.portal import AuthError, Health
from editor.service import EXIT_RESTART
from editor.updater import EXIT_UPDATED, RestartForUpdate
from editor.worker import LoopSettings, RestartRequested, Worker  # noqa: F401 - re-exported

log = logging.getLogger("editor")
EXIT_AUTH = 3


def _pool(cfg, keychain):
    from editor.backends import BackendPool
    from editor.backends.claude import ClaudeBackend
    from editor.backends.codex import CodexBackend
    from editor.backends.grok import GrokBackend

    # All three exist; config `agents` is only the order until the Portal's agentOrder arrives.
    return BackendPool([ClaudeBackend(cfg, keychain), CodexBackend(cfg), GrokBackend(cfg)], cfg.agents)


def build_worker():
    """Wire the real pieces together."""
    from functools import partial

    from editor.commands import CommandRunner
    from editor.config import Config
    from editor.doctor import Probes, check
    from editor.drive import DriveClient
    from editor.housekeeping import cleanup_job_dirs
    from editor.keychain import Keychain
    from editor.link import TOKEN_ITEM, device_name
    from editor.logout_guard import logout_guard
    from editor.pairing import Pairing
    from editor.portal import PortalClient
    from editor.runner import RunnerDeps, run_job
    from editor.service import reload_if_changed
    from editor.shell import RemoteShell
    from editor.updater import Updater

    cfg = Config.load()
    keychain = Keychain()
    pool = _pool(cfg, keychain)
    pairing = Pairing(keychain, pool, cfg.codex_home, cfg.grok_home)
    portal = PortalClient(cfg.portal_url, lambda: keychain.get(TOKEN_ITEM))
    deps = RunnerDeps(cfg=cfg, portal=portal, drive=DriveClient(), pool=pool)
    probes = Probes(connected_backends=lambda: [b.name for b in pool.connected()],
                    linked=lambda: bool(keychain.get(TOKEN_ITEM)))
    updater = None
    if cfg.auto_update:
        updater = Updater(cfg.repo_root, cfg.data_dir, update_repo=cfg.update_repo,
                          require_signed_tags=cfg.require_signed_tags).check
    return Worker(portal=portal, version=cfg.version, doctor=partial(check, cfg, probes),
                  run_job=partial(run_job, deps), update=updater, pairing=pairing,
                  backend_statuses=pool.statuses, set_agent_order=pool.set_order,
                  commands=CommandRunner(cfg.repo_root, cfg.data_dir, work_root=cfg.work_root),
                  machine={"name": device_name(), "kind": cfg.machine_kind},
                  before_exit=lambda: reload_if_changed(cfg.data_dir),
                  session_guard=partial(logout_guard, cfg.data_dir, cfg.machine_kind),
                  shell=RemoteShell(cfg.machine_kind, lambda: keychain.get(TOKEN_ITEM)),
                  housekeeping=lambda: cleanup_job_dirs(cfg.work_root, cfg.keep_days))


def _doctor_health() -> tuple[Health, tuple[str, ...]]:
    from editor.config import Config, ConfigError
    from editor.doctor import Probes, check
    from editor.keychain import Keychain
    from editor.link import TOKEN_ITEM, link_status

    try:
        cfg = Config.load()
    except ConfigError as e:
        return Health(False, (str(e),)), ()
    keychain = Keychain()
    pool = _pool(cfg, keychain)
    token = keychain.get(TOKEN_ITEM)
    status = link_status(cfg.portal_url, token, machine_kind=cfg.machine_kind)
    health = check(cfg, Probes(connected_backends=lambda: [b.name for b in pool.connected()],
                               linked=lambda: status != "unlinked"))
    lines = {"standby": ("Linked, standby (another machine is active).",),
             "active": ("Linked: this Mac is the active editor.",),
             "unreachable": ("Couldn't reach the Portal to check the link.",)}.get(status, ())
    return health, lines


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infocus-editor")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="the worker loop (started by launchd)")
    doctor = sub.add_parser("doctor", help="health check")
    doctor.add_argument("--json", action="store_true", help="print {ok, problems} as JSON")
    link = sub.add_parser("link", help="link this Mac to the Portal with a device code")
    link.add_argument("--progress", action="store_true", help="print @@ progress lines for the setup app")
    link.add_argument("--check", action="store_true", help="exit 0 if the saved link works")
    conn = sub.add_parser("connect", help="sign in to an AI service on this computer and send it to the editing Mac")
    conn.add_argument("backend", choices=("claude", "codex", "grok"))
    conn.add_argument("--code", required=True, help="the pairing code from the Portal")
    conn.add_argument("--portal", required=True, help="the Portal address")
    conn.add_argument("--device-auth", action="store_true", help="sign in with a device code instead of a browser")
    args = parser.parse_args(argv)
    command = args.command or "run"

    if command == "connect":
        from editor.connect import ConnectError, connect

        try:
            connect(args.backend, args.code, args.portal, device_auth=args.device_auth)
        except ConnectError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1
        return 0
    if command == "link":
        from editor.link import cli_link

        return cli_link(progress=args.progress, check=args.check)

    from editor.joblog import ScrubFormatter
    from editor.logs import quiet_http_logs

    stderr = logging.StreamHandler()  # launchd sends it to ~/Library/Logs/infocus-editor.log
    stderr.setFormatter(ScrubFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))  # never a token
    logging.basicConfig(level=logging.INFO, handlers=[stderr])
    quiet_http_logs()  # no line per heartbeat; failed requests still show

    if command == "doctor":
        from editor.commands import health_report

        health, lines = _doctor_health()
        if args.json:
            print(json.dumps(health.as_json()))
        else:
            print(health_report(health, lines))
        return 0 if health.ok else 1

    from editor.config import ConfigError

    try:
        worker = build_worker()
        asyncio.run(worker.serve())
    except ConfigError as e:
        log.error("%s", e)
        return 2
    except AuthError as e:  # not expected: the worker handles 401 by waiting to be linked again
        log.error("%s", e)
        return EXIT_AUTH
    except RestartForUpdate as e:
        log.info("%s Restarting on the new code.", e)
        return EXIT_UPDATED
    except RestartRequested as e:
        log.info("Restarting (%s).", e)
        return EXIT_RESTART
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(cli())
