"""Remote self-update with no admin: move the production checkout to a release
tag, re-run user-setup.sh --update, check health, roll back on failure.

The target is the Portal's pin (heartbeat `targetVersion`) if set, else the
latest GitHub release. Runs only while the worker is idle; on success the
worker exits with EXIT_UPDATED and launchd starts it again on the new code.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import httpx

EXIT_UPDATED = 10
ADMIN_NEEDED = "An update needs admin approval: open InFocus Editor Setup and choose Repair."
INSTALLED_HELPER = Path("/Library/InFocusEditor/bin/infocus-root")
CHECK_EVERY_S = 15 * 60
SETUP_TIMEOUT_S = 45 * 60
TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.-]+)?$")

Run = Callable[..., subprocess.CompletedProcess]


class RestartForUpdate(Exception):
    """Raised by the worker loop after a successful update."""


@dataclass(frozen=True)
class UpdateResult:
    status: str  # current | updated | rolled_back | refused | check_failed
    target: str | None = None
    message: str = ""

    @property
    def problem(self) -> str | None:
        """The sentence for heartbeat health, if producers should see it."""
        return self.message if self.status in ("rolled_back", "refused") else None


def latest_release_tag(update_repo: str, transport: httpx.BaseTransport | None = None) -> str | None:
    url = f"https://api.github.com/repos/{update_repo}/releases/latest"
    with httpx.Client(transport=transport, timeout=20, headers={"Accept": "application/vnd.github+json"}) as c:
        res = c.get(url)
    if res.status_code == 404:
        return None  # no releases yet
    res.raise_for_status()
    return str(res.json().get("tag_name") or "") or None


class Updater:
    def __init__(self, repo_dir: Path, data_dir: Path, *, update_repo: str, require_signed_tags: bool = False,
                 run: Run = subprocess.run, latest: Callable[[], str | None] | None = None,
                 installed_helper: Callable[[], str | None] | None = None):
        self._repo = repo_dir
        self._data = data_dir
        self._signed = require_signed_tags
        self._run = run
        self._latest = latest or (lambda: latest_release_tag(update_repo))
        self._failed: set[str] = set()
        self._installed_helper = installed_helper or (lambda: helper_version_text(_read(INSTALLED_HELPER)))

    # ---- git helpers ------------------------------------------------------

    def _git(self, *args: str, timeout: float = 300) -> subprocess.CompletedProcess:
        return self._run(["git", "-C", str(self._repo), *args], capture_output=True, text=True,
                         check=False, timeout=timeout)

    def current_version(self) -> str:
        proc = self._git("describe", "--tags", "--always")
        return (proc.stdout.strip() if proc.returncode == 0 else "unknown")[:40]

    def current_tag(self) -> str | None:
        proc = self._git("describe", "--tags", "--exact-match", "HEAD")
        return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None

    # ---- the update -------------------------------------------------------

    def check(self, pinned: str | None, problems_before: Sequence[str] = ()) -> UpdateResult:
        try:
            target = pinned or self._latest()
        except Exception as e:  # noqa: BLE001 - a network blip just waits for the next check
            return UpdateResult("check_failed", None, f"Couldn't check for updates: {e}"[:300])
        if not target:
            return UpdateResult("current")
        if not TAG_RE.match(target):
            return UpdateResult("refused", target, f"Ignoring target version {target!r}: not a release tag.")
        if target == self.current_tag():
            return UpdateResult("current", target)
        if target in self._failed:
            return UpdateResult("refused", target, f"Update to {target} failed earlier; staying on "
                                                   f"{self.current_version()} until the target changes.")
        result = self._update_to(target, problems_before)
        if result.status != "updated":
            self._failed.add(target)
        return result

    def _update_to(self, target: str, problems_before: Sequence[str]) -> UpdateResult:
        if self._git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            return UpdateResult("refused", target, f"Update to {target} skipped: the app folder has local changes.")
        fetched = self._git("fetch", "--tags", "--force", "--quiet", "origin")
        if fetched.returncode != 0:
            return UpdateResult("check_failed", target, f"Couldn't fetch {target}: {fetched.stderr.strip()[:200]}")
        if not self._git("ls-remote", "--tags", "origin", f"refs/tags/{target}").stdout.strip():
            return UpdateResult("refused", target, f"Update to {target} failed: there's no such tag on GitHub.")
        if self._signed and self._git("verify-tag", target).returncode != 0:
            return UpdateResult("refused", target, f"Update to {target} failed: the tag isn't signed by a trusted key.")
        # Checked before checkout: a release with a new root helper can't install without an admin.
        installed = self._installed_helper()
        wanted = helper_version_text(self._git("show", f"{target}:install/infocus-root").stdout)
        if installed is not None and wanted is not None and wanted != installed:
            return UpdateResult("refused", target, f"{ADMIN_NEEDED} ({target} changes the admin helper.)")
        previous = self.current_tag() or self._git("rev-parse", "HEAD").stdout.strip()
        checkout = self._git("checkout", "--quiet", "--detach", target)
        if checkout.returncode != 0:
            return UpdateResult("refused", target, f"Update to {target} failed: {checkout.stderr.strip()[:200]}")
        reason = self._setup() or self._doctor(problems_before)
        if reason is None:
            return UpdateResult("updated", target, f"Updated to {target}.")
        return self._rollback(target, previous, reason)

    def _rollback(self, target: str, previous: str, reason: str) -> UpdateResult:
        message = f"Update to {target} failed: {reason}"
        back = self._git("checkout", "--quiet", "--detach", previous)
        again = self._setup() if back.returncode == 0 else back.stderr.strip()[:200]
        if back.returncode != 0 or again:
            message += f" Rolling back to {previous} also failed: {again}"
        else:
            message += f" Rolled back to {previous}."
        return UpdateResult("rolled_back", target, message[:300])

    def _env(self) -> dict[str, str]:
        return {**os.environ, "INFOCUS_EDITOR_HOME": str(self._data)}

    def _setup(self) -> str | None:
        script = self._repo / "install" / "user-setup.sh"
        try:
            proc = self._run([str(script), "--update", "--unattended", "--home", str(self._data)], capture_output=True,
                             text=True, check=False, timeout=SETUP_TIMEOUT_S, env=self._env())
        except subprocess.TimeoutExpired:
            return "setup timed out."
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
            return f"setup failed ({tail[0][:150]})."
        return None

    def _doctor(self, problems_before: Sequence[str]) -> str | None:
        """New code must start and add no new health problems."""
        cmd = ["uv", "run", "--project", str(self._repo), "infocus-editor", "doctor", "--json"]
        try:
            proc = self._run(cmd, capture_output=True, text=True, check=False, timeout=600, env=self._env())
            report = json.loads(proc.stdout.strip().splitlines()[-1])
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            return "the new version's health check didn't run."
        new = [p for p in report.get("problems") or [] if p not in set(problems_before)]
        if new:
            return f"health check: {new[0]}"
        return None


def helper_version_text(text: str | None) -> str | None:
    for line in (text or "").splitlines():
        if line.startswith("HELPER_VERSION="):
            return line.split("=", 1)[1].strip() or None
    return None


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None
