"""Auto-update end to end with real git (no network): a local bare repo plays GitHub.
Only the GitHub "latest release" lookup, user-setup.sh and the doctor are fakes.

Design: the root-helper check runs BEFORE checkout (Updater._update_to compares
`git show <tag>:install/infocus-root` with the installed helper), so a release that
needs an admin is never checked out at all.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from editor.config import REPO_ROOT
from editor.portal import Health
from editor.updater import ADMIN_NEEDED, EXIT_UPDATED, RestartForUpdate, Updater
from editor.worker import LoopSettings, Worker

GIT = ["git", "-c", "user.name=Release Bot", "-c", "user.email=bot@example.org", "-c", "init.defaultBranch=main"]

FAKE_SETUP = """#!/bin/bash
echo "setup $(cat "$(dirname "$0")/../VERSION") $*" >> "$UPDATE_FLOW_LOG"
"""


def git(*args, cwd=None):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Releases:
    """A work tree that publishes tags to the bare "GitHub" repo."""

    def __init__(self, tmp: Path):
        self.bare = tmp / "github" / "infocus-editor.git"
        self.work = tmp / "release-work"
        git("init", "-q", "--bare", str(self.bare))
        git("init", "-q", str(self.work))
        git("remote", "add", "origin", str(self.bare), cwd=self.work)
        (self.work / "install").mkdir()
        setup = self.work / "install" / "user-setup.sh"
        setup.write_text(FAKE_SETUP)
        setup.chmod(0o755)
        self.latest = None

    def release(self, version: str, helper_version: int = 1) -> None:
        (self.work / "VERSION").write_text(version.lstrip("v"))
        (self.work / "install" / "infocus-root").write_text(f"#!/bin/bash\nHELPER_VERSION={helper_version}\n")
        git("add", ".", cwd=self.work)
        git("commit", "-q", "-m", f"release {version}", cwd=self.work)
        git("tag", "-a", version, "-m", version, cwd=self.work)
        git("push", "-q", "origin", "HEAD:main", version, cwd=self.work)
        self.latest = version


@pytest.fixture
def flow(tmp_path, monkeypatch):
    log = tmp_path / "flow.log"
    monkeypatch.setenv("UPDATE_FLOW_LOG", str(log))
    releases = Releases(tmp_path)
    releases.release("v0.1.0")
    data = tmp_path / "InFocusEditor"
    data.mkdir()
    app = data / "app"
    doctor_problems = {}

    # 1. Install from the setup app's bundle, exactly as install.sh does (lib.sh clone_app).
    bundle = tmp_path / "infocus-editor.bundle"
    git("bundle", "create", str(bundle), "--all", cwd=releases.work)
    subprocess.run(["bash", "-c", f'. "{REPO_ROOT}/install/lib.sh"; clone_app "$0" "$1"', str(bundle), str(app)],
                   env={**os.environ, "INFOCUS_DEFAULT_REPO_URL": str(releases.bare)}, check=True,
                   capture_output=True)

    def run(cmd, **kw):
        if cmd[:2] == ["uv", "run"]:  # the new version's doctor
            version = (app / "VERSION").read_text()
            with log.open("a") as fh:
                fh.write(f"doctor {version}\n")
            problems = doctor_problems.get(version, [])
            return subprocess.CompletedProcess(cmd, 1 if problems else 0,
                                               json.dumps({"ok": not problems, "problems": problems}), "")
        return subprocess.run(cmd, **kw)

    updater = Updater(app, data, update_repo="example/infocus-editor", run=run,
                      latest=lambda: releases.latest, installed_helper=lambda: "1")
    return {"releases": releases, "app": app, "data": data, "log": log, "updater": updater,
            "doctor_problems": doctor_problems}


def on(app: Path) -> str:
    return git("describe", "--tags", "--exact-match", "HEAD", cwd=app)


def calls(flow) -> list[str]:
    return flow["log"].read_text().splitlines() if flow["log"].exists() else []


def worker(flow, *, target=None, busy=False):
    w = Worker(portal=None, version=on(flow["app"]), doctor=lambda: Health(True), run_job=None,
               update=flow["updater"].check, settings=LoopSettings(update_s=0))
    w.target_version = target
    w.busy = busy
    return w


async def test_full_update_flow(flow):
    app, releases = flow["app"], flow["releases"]

    # 1. Installed from the bundle: on the newest tag, with origin pointing at "GitHub".
    assert on(app) == "v0.1.0"
    assert git("remote", "get-url", "origin", cwd=app) == str(releases.bare)

    # 2. A new release: one check moves to it, runs setup --update and doctor, then asks for a restart.
    releases.release("v0.1.1")
    with pytest.raises(RestartForUpdate):
        await worker(flow).maybe_update()
    assert on(app) == "v0.1.1" and EXIT_UPDATED == 10
    assert calls(flow) == [f"setup 0.1.1 --update --unattended --home {flow['data']}", "doctor 0.1.1"]

    # 3. The Portal pins v0.1.0: it rolls back to it.
    with pytest.raises(RestartForUpdate):
        await worker(flow, target="v0.1.0").maybe_update()
    assert on(app) == "v0.1.0"

    # 4. v0.1.2's doctor reports a new problem: rolled back, reported, not retried until the target changes.
    flow["doctor_problems"]["0.1.2"] = ["The speech model is missing."]
    releases.release("v0.1.2")
    w = worker(flow)
    await w.maybe_update()
    assert on(app) == "v0.1.0"
    assert w.update_problem.startswith("Update to v0.1.2 failed: health check: The speech model is missing.")
    assert "Rolled back to v0.1.0" in w.update_problem
    before = len(calls(flow))
    result = flow["updater"].check(None)
    assert result.status == "refused" and "failed earlier" in result.message and len(calls(flow)) == before
    assert flow["updater"].check("v0.1.1").status == "updated" and on(app) == "v0.1.1"  # a new target is tried

    # 5. While a job runs, nothing updates.
    releases.release("v0.1.4")
    busy = worker(flow, busy=True)
    await busy.maybe_update()
    assert on(app) == "v0.1.1"

    # 6. A release that changes the root helper is not applied; health says an admin is needed.
    releases.release("v0.1.5", helper_version=2)
    w = worker(flow)
    await w.maybe_update()
    assert on(app) == "v0.1.1"
    assert w.update_problem.startswith(ADMIN_NEEDED)
    assert ADMIN_NEEDED == "An update needs admin approval: open InFocus Editor Setup and choose Repair."
    assert w.reported_health().problems[-1] == w.update_problem


def test_the_check_runs_before_checkout_so_nothing_is_touched(flow):
    releases, app = flow["releases"], flow["app"]
    releases.release("v0.2.0", helper_version=2)
    head = git("rev-parse", "HEAD", cwd=app)
    result = flow["updater"].check(None)
    assert result.status == "refused" and git("rev-parse", "HEAD", cwd=app) == head
    assert not any(line.startswith("setup") for line in calls(flow))
