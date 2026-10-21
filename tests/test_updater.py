import json
import subprocess
from pathlib import Path

import httpx

from editor.updater import Updater, latest_release_tag

REPO = Path("/data/app")
DATA = Path("/data")


class FakeGit:
    """A fake `git` + user-setup.sh + doctor, enough for the updater."""

    def __init__(self, head="v0.1.0", remote_tags=("v0.1.0", "v0.2.0", "v0.3.0"), signed=(), dirty=False,
                 setup_fails_on=(), doctor_problems=None):
        self.head = head
        self.remote_tags = set(remote_tags)
        self.signed = set(signed)
        self.dirty = dirty
        self.setup_fails_on = set(setup_fails_on)
        self.doctor_problems = doctor_problems or {}
        self.log = []

    def __call__(self, cmd, **kw):
        ok = lambda out="": subprocess.CompletedProcess(cmd, 0, out, "")  # noqa: E731
        bad = lambda err="": subprocess.CompletedProcess(cmd, 1, "", err)  # noqa: E731
        if cmd[0] == "git":
            args = cmd[3:]
            self.log.append(" ".join(args[:2]))
            if args[:2] == ["describe", "--tags"]:
                return ok(self.head + "\n")
            if args[0] == "status":
                return ok(" M editor/main.py\n" if self.dirty else "")
            if args[0] == "fetch":
                return ok()
            if args[0] == "ls-remote":
                tag = args[-1].removeprefix("refs/tags/")
                return ok(f"abc\trefs/tags/{tag}\n" if tag in self.remote_tags else "")
            if args[0] == "verify-tag":
                return ok() if args[1] in self.signed else bad("no signature found")
            if args[0] == "show":
                return ok("HELPER_VERSION=1\n")
            if args[0] == "rev-parse":
                return ok("deadbeef\n")
            if args[0] == "checkout":
                self.head = args[-1]
                self.log.append(f"checkout {args[-1]}")
                return ok()
        if cmd[0].endswith("user-setup.sh"):
            assert cmd[1:] == ["--update", "--unattended", "--home", str(DATA)]
            self.log.append(f"setup@{self.head}")
            return bad("brew failed") if self.head in self.setup_fails_on else ok()
        if cmd[:2] == ["uv", "run"]:
            problems = self.doctor_problems.get(self.head, [])
            self.log.append(f"doctor@{self.head}")
            return subprocess.CompletedProcess(cmd, 1 if problems else 0,
                                               json.dumps({"ok": not problems, "problems": problems}), "")
        raise AssertionError(cmd)


def updater(git, latest="v0.3.0", signed=False):
    return Updater(REPO, DATA, update_repo="example/infocus-editor", require_signed_tags=signed,
                   run=git, latest=lambda: latest, installed_helper=lambda: "1")


def test_follows_the_latest_release():
    git = FakeGit()
    result = updater(git).check(None)
    assert result.status == "updated" and result.target == "v0.3.0"
    assert git.head == "v0.3.0"
    assert git.log.index("checkout v0.3.0") < git.log.index("setup@v0.3.0") < git.log.index("doctor@v0.3.0")


def test_already_current_does_nothing():
    git = FakeGit(head="v0.3.0")
    assert updater(git).check(None).status == "current"
    assert "fetch --tags" not in git.log


def test_the_pin_wins_over_the_latest_release():
    git = FakeGit(head="v0.3.0")
    result = updater(git, latest="v0.3.0").check("v0.2.0")
    assert result.status == "updated" and git.head == "v0.2.0"  # a pin can roll back too


def test_rollback_on_doctor_failure():
    git = FakeGit(doctor_problems={"v0.3.0": ["The one-time admin step is missing or out of date."]})
    result = updater(git).check(None, problems_before=())
    assert result.status == "rolled_back" and git.head == "v0.1.0"
    assert result.problem.startswith("Update to v0.3.0 failed: health check: The one-time admin step")
    assert "Rolled back to v0.1.0" in result.message
    assert git.log[-1] == "setup@v0.1.0"


def test_existing_problems_dont_cause_a_rollback():
    git = FakeGit(doctor_problems={"v0.3.0": ["The MacBook is on battery; plug it in."]})
    result = updater(git).check(None, problems_before=("The MacBook is on battery; plug it in.",))
    assert result.status == "updated"


def test_rollback_on_setup_failure_and_no_retry_of_the_same_target():
    git = FakeGit(setup_fails_on={"v0.3.0"})
    u = updater(git)
    first = u.check(None)
    assert first.status == "rolled_back" and "setup failed (brew failed)" in first.message
    assert git.head == "v0.1.0"
    second = u.check(None)
    assert second.status == "refused" and "failed earlier" in second.message
    assert git.log.count("setup@v0.3.0") == 1


def test_signed_tag_check():
    git = FakeGit()
    result = updater(git, signed=True).check(None)
    assert result.status == "refused" and "isn't signed" in result.message and git.head == "v0.1.0"
    git2 = FakeGit(signed={"v0.3.0"})
    assert updater(git2, signed=True).check(None).status == "updated"


def test_tag_missing_on_origin_is_refused():
    git = FakeGit(remote_tags={"v0.1.0"})
    result = updater(git).check("v0.9.0")
    assert result.status == "refused" and "no such tag" in result.message and git.head == "v0.1.0"


def test_local_changes_block_the_update():
    git = FakeGit(dirty=True)
    result = updater(git).check(None)
    assert result.status == "refused" and "local changes" in result.message and git.head == "v0.1.0"


def test_a_target_that_isnt_a_tag_is_refused():
    git = FakeGit()
    result = updater(git).check("main; rm -rf ~")
    assert result.status == "refused" and git.log == []


def test_failed_release_check_is_quiet():
    def boom():
        raise httpx.ConnectError("offline")

    u = Updater(REPO, DATA, update_repo="x/y", run=FakeGit(), latest=boom, installed_helper=lambda: "1")
    result = u.check(None)
    assert result.status == "check_failed" and result.problem is None


def test_latest_release_tag_reads_github():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"tag_name": "v0.4.2"})

    assert latest_release_tag("example/infocus-editor", httpx.MockTransport(handler)) == "v0.4.2"
    assert seen == ["https://api.github.com/repos/example/infocus-editor/releases/latest"]
    assert latest_release_tag("x/y", httpx.MockTransport(lambda r: httpx.Response(404))) is None
