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
    ["update-clt", "everything"],
])
def test_rejects_unknown_subcommands_and_bad_arguments(env, args):
    assert helper(env, *args).returncode == 2


def test_apply_power_uses_the_fixed_values(env):
    out = helper(env, "apply-power")
    assert out.returncode == 0
    assert out.stdout.strip() == "would run: pmset -c sleep 0 disksleep 0 autorestart 1 womp 1"


def test_refuses_to_run_without_sudo_or_dry_run(env):
    clean = {k: v for k, v in env.items() if k != "INFOCUS_ROOT_DRY_RUN"}
    out = subprocess.run([str(HELPER), "status"], env=clean, capture_output=True, text=True, check=False)
    assert out.returncode == 2 and "through sudo" in out.stderr


def test_helper_declares_its_version():
    assert "HELPER_VERSION=1" in Path(HELPER).read_text().splitlines()
