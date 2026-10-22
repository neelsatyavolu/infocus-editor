"""infocus-root argument validation, in dry-run mode (no root)."""
import os
import subprocess
from pathlib import Path

import pytest

from editor.config import REPO_ROOT

HELPER = REPO_ROOT / "install" / "infocus-root"


@pytest.fixture
def env():
    return {**os.environ, "INFOCUS_ROOT_DRY_RUN": "1"}


def helper(env, *args):
    return subprocess.run([str(HELPER), *args], env=env, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("args", [
    [],
    ["shell"],
    ["install-pkg", "/tmp/Install Resolve.pkg"],
    ["sh", "-c", "id"],
    ["status", "extra"],
    ["apply-power", "--now"],
    ["always-on"],
    ["always-on", "maybe"],
    ["always-on", "on", "now"],
    ["always-on", "on; id"],
    ["update-clt", "everything"],
])
def test_rejects_unknown_subcommands_and_bad_arguments(env, args):
    assert helper(env, *args).returncode == 2


ALWAYS_ON = [
    "would run: pmset -a sleep 0 disksleep 0 standby 0 disablesleep 1 autorestart 1 womp 1",
    "would run: defaults write /Library/Preferences/.GlobalPreferences com.apple.autologout.AutoLogOutDelay -int 0",
    "would run: defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false",
]


@pytest.mark.parametrize("args", [["apply-power"], ["always-on", "on"]])
def test_always_on_never_sleeps_even_with_the_lid_closed_and_blocks_logout_and_update_restarts(env, args):
    out = helper(env, *args)
    assert out.returncode == 0
    assert out.stdout.strip().splitlines() == ALWAYS_ON


def test_always_on_off_goes_back_to_the_macos_defaults(env):
    out = helper(env, "always-on", "off")
    assert out.returncode == 0
    assert out.stdout.strip().splitlines() == [
        "would run: pmset -a disablesleep 0",
        "would run: pmset restoredefaults",
        "would run: defaults delete /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates",
    ]


def test_refuses_to_run_without_sudo_or_dry_run(env):
    clean = {k: v for k, v in env.items() if k != "INFOCUS_ROOT_DRY_RUN"}
    out = subprocess.run([str(HELPER), "status"], env=clean, capture_output=True, text=True, check=False)
    assert out.returncode == 2 and "through sudo" in out.stderr


def test_helper_declares_its_version():
    assert "HELPER_VERSION=2" in Path(HELPER).read_text().splitlines()
