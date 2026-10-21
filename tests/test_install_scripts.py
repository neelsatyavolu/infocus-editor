"""Static checks on the install scripts. They are never executed here (they install software)."""
import shutil
import subprocess

import pytest

from editor.config import REPO_ROOT

SCRIPTS = sorted((REPO_ROOT / "install").glob("*.sh")) + [REPO_ROOT / "install" / "infocus-root",
                                                          REPO_ROOT / "scripts" / "release.sh"]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script):
    # /bin/bash is bash 3.2 on macOS, which the setup app uses.
    assert subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    out = subprocess.run(["shellcheck", "-x", *map(str, SCRIPTS)], capture_output=True, text=True,
                         cwd=REPO_ROOT)
    assert out.returncode == 0, out.stdout


def lib(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'. "{REPO_ROOT}/install/lib.sh"; {snippet}'],
                          capture_output=True, text=True)


def test_admin_dialog_refuses_paths_with_quotes():
    assert lib("plain_path '/Users/editor/InFocusEditor/app'").returncode == 0
    assert lib("plain_path \"/tmp/it's\"").returncode == 1
    assert lib("plain_path '/tmp/a\"b'").returncode == 1
    out = lib("run_as_admin /bin/echo \"/tmp/it's\"")
    assert out.returncode == 1 and "admin dialog" in out.stderr


def test_sudoers_rule_names_only_the_helper():
    text = (REPO_ROOT / "install" / "bootstrap-root.sh").read_text()
    assert 'ALL=(root) NOPASSWD: %s' in text and "visudo -cf" in text and "-m 440" in text
    assert "NOPASSWD: ALL" not in text


def test_helper_versions_parse_the_same_in_shell_and_python():
    from editor.doctor import helper_version

    out = lib(f'helper_version_of "{REPO_ROOT}/install/infocus-root"')
    assert out.stdout.strip() == helper_version(REPO_ROOT / "install" / "infocus-root") == "1"
