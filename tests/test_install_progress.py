"""Run the real install.sh / user-setup.sh against fakes (brew, osascript, sudo, uv, launchctl …)
and check the @@ progress protocol the setup app reads (docs/setup-app.md). Nothing real is
installed: Homebrew, the root folder and the sudoers file are temporary stand-ins."""
import getpass
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from editor.config import REPO_ROOT

PORTAL = "https://portal.example.org"
WHISPER_SIZE = 1624555275


def script(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake = tmp_path / "bin"
    fake.mkdir()
    log = tmp_path / "calls.log"
    brew_prefix = tmp_path / "homebrew"
    (brew_prefix / "bin").mkdir(parents=True)
    root_dir = tmp_path / "LibraryInFocusEditor"

    # The production checkout, already cloned (a real git repo of the files the scripts use).
    data = home / "InFocusEditor"
    app = data / "app"
    app.mkdir(parents=True)
    for name in ("install", "launchd"):
        shutil.copytree(REPO_ROOT / name, app / name)
    for name in ("config.example.toml", "house-rules.example.md"):
        shutil.copy(REPO_ROOT / name, app / name)
    git = ["git", "-C", str(app), "-c", "user.name=t", "-c", "user.email=t@example.org"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "app"], check=True)

    # The speech model is already there (a sparse file of the right size).
    model = home / ".cache" / "whisper-cpp" / "ggml-large-v3-turbo.bin"
    model.parent.mkdir(parents=True)
    with model.open("wb") as fh:
        fh.truncate(WHISPER_SIZE)

    package_src = tmp_path / "package-src"
    (package_src / "03 Intro & Outro").mkdir(parents=True)
    (package_src / "03 Intro & Outro" / "InFocus Intro 2026.mp4").write_bytes(b"x")

    script(brew_prefix / "bin" / "brew", f'echo "brew $*" >> "{log}"; exit ${{FAKE_BREW_FAIL:-0}}\n')
    script(fake / "osascript", f'''
        echo "osascript $*" >> "{log}"
        [ "${{FAKE_ADMIN_CANCEL:-0}}" = 1 ] && {{ echo "User canceled." >&2; exit 1; }}
        mkdir -p "$INFOCUS_ROOT_DIR/bin"
        cp "{app}/install/infocus-root" "$INFOCUS_ROOT_DIR/bin/infocus-root"
        echo "helper_version=1" > "$INFOCUS_ROOT_DIR/bootstrap.done"
        touch "$INFOCUS_SUDOERS"
    ''')
    script(fake / "sudo", '[ "$1" = -n ] && shift; INFOCUS_ROOT_DRY_RUN=1 "$@" >/dev/null\n')
    script(fake / "uv", f'''
        echo "uv $*" >> "{log}"
        case "$*" in
          *"link --check"*) [ "${{FAKE_LINKED:-0}}" = 1 ] && exit 0; exit 1 ;;
          *"link --progress"*) echo "@@link K7Q2-M9XD {PORTAL}/show-roles?link=K7Q2-M9XD"; echo "@@linked" ;;
          *"link"*) echo "Open {PORTAL}/show-roles?link=K7Q2-M9XD and approve this Mac." ;;
          *doctor*) echo "Healthy: ready to edit." ;;
        esac
        exit 0
    ''')
    for name in ("npm", "npx", "node", "launchctl", "security", "curl", "xcode-select"):
        script(fake / name, f'echo "{name} $*" >> "{log}"\n')
    env = {
        "HOME": str(home),
        "PATH": f"{fake}:/usr/bin:/bin:/usr/sbin:/sbin",
        "INFOCUS_ROOT_DIR": str(root_dir),
        "INFOCUS_SUDOERS": str(tmp_path / "sudoers-infocus-editor"),
        "INFOCUS_ROOT_OWNER": getpass.getuser(),
        "INFOCUS_BREW_PREFIX": str(brew_prefix),
        "LANG": "C",
    }
    return {"env": env, "fake": fake, "log": log, "data": data, "app": app, "package_src": package_src,
            "home": home, "root_dir": root_dir}


def install(sb, *extra, env=None):
    cmd = ["/bin/bash", str(REPO_ROOT / "install" / "install.sh"), "--unattended", "--progress",
           "--home", str(sb["data"]), "--package-src", str(sb["package_src"]), "--portal", PORTAL, *extra]
    proc = subprocess.run(cmd, env={**sb["env"], **(env or {})}, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=120)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("@@")]
    return proc, lines


def steps(lines):
    return [tuple(l.split(" ", 3)[1:3]) for l in lines if l.startswith("@@step")]


def add_tools(sb, *names):
    for name in names:
        script(sb["fake"] / name, f'echo "{name} $*" >> "{sb["log"]}"\n')


def test_dedicated_install_reports_every_step_and_ends_with_done(sandbox):
    proc, lines = install(sandbox)
    assert proc.returncode == 0, proc.stderr
    assert lines[-1] == "@@done"
    assert steps(lines) == [
        ("admin", "running"), ("admin", "done"), ("clt", "skipped"), ("homebrew", "skipped"),
        ("app", "running"),
        ("tools", "running"), ("tools", "done"),
        ("ai", "running"), ("ai", "done"),
        ("model", "running"), ("model", "skipped"),
        ("app", "running"), ("app", "done"),
        ("package", "running"), ("package", "done"),
        ("link", "running"), ("link", "done"),
        ("service", "running"), ("service", "done"),
        ("health", "running"), ("health", "done"),
    ]
    assert f"@@link K7Q2-M9XD {PORTAL}/show-roles?link=K7Q2-M9XD" in lines and "@@linked" in lines
    config = (sandbox["data"] / "config.toml").read_text()
    assert f'portal_url = "{PORTAL}"' in config and 'machine_kind = "dedicated"' in config
    assert (sandbox["data"] / "package" / "03 Intro & Outro" / "InFocus Intro 2026.mp4").exists()
    plist = (sandbox["home"] / "Library" / "LaunchAgents" / "com.infocus.editor.plist").read_text()
    assert "/usr/bin/caffeinate" in plist and "__" not in plist
    calls = sandbox["log"].read_text()
    assert "brew bundle --no-upgrade --file" in calls and "launchctl bootstrap" in calls
    assert "--personal" not in calls


def test_a_failing_step_ends_with_error_and_a_nonzero_exit(sandbox):
    proc, lines = install(sandbox, env={"FAKE_BREW_FAIL": "1"})
    assert proc.returncode != 0
    assert lines[-2] == "@@step tools failed Homebrew couldn't install the video tools."
    assert lines[-1] == "@@error Homebrew couldn't install the video tools."
    assert "@@done" not in lines


def test_cancelled_admin_dialog_is_reported(sandbox):
    proc, lines = install(sandbox, env={"FAKE_ADMIN_CANCEL": "1"})
    assert proc.returncode != 0
    assert lines[-2].startswith("@@step admin failed The admin step didn't finish")
    assert lines[-1].startswith("@@error The admin step didn't finish")


def test_second_run_skips_the_admin_step_and_linking(sandbox):
    install(sandbox)
    proc, lines = install(sandbox, env={"FAKE_LINKED": "1"})
    assert proc.returncode == 0 and lines[-1] == "@@done"
    assert ("admin", "skipped") in steps(lines) and ("link", "skipped") in steps(lines)
    assert sandbox["log"].read_text().count("osascript") == 1


def test_personal_computer_reuses_tools_and_skips_power_and_caffeinate(sandbox):
    add_tools(sandbox, "ffmpeg", "whisper-cli", "codex", "grok")  # git, uv, node are already faked
    proc, lines = install(sandbox, "--personal")
    assert proc.returncode == 0, proc.stderr
    assert ("tools", "skipped") in steps(lines)
    calls = sandbox["log"].read_text()
    assert "brew " not in calls  # nothing installed or upgraded through Homebrew
    assert "bootstrap-root.sh" in calls and "'--personal'" in calls
    plist = (sandbox["home"] / "Library" / "LaunchAgents" / "com.infocus.editor.plist").read_text()
    assert "caffeinate" not in plist
    subprocess.run(["plutil", "-lint", "-"], input=plist, text=True, check=True, capture_output=True)
    assert 'machine_kind = "personal"' in (sandbox["data"] / "config.toml").read_text()


def test_personal_computer_installs_only_missing_tools(sandbox):
    add_tools(sandbox, "codex", "grok", "whisper-cli")
    proc, lines = install(sandbox, "--personal")
    assert proc.returncode == 0, proc.stderr
    brew_calls = [l for l in sandbox["log"].read_text().splitlines() if l.startswith("brew ")]
    assert brew_calls == ["brew install ffmpeg"]


def test_homebrew_owned_by_another_account_stops_personal_setup(sandbox):
    proc, lines = install(sandbox, "--personal", env={"INFOCUS_BREW_PREFIX": "/usr"})
    assert proc.returncode != 0
    assert lines[-1] == ("@@error Homebrew on this Mac belongs to another account (root). "
                         "Run setup from that account, or ask an admin.")
    assert "osascript" not in (sandbox["log"].read_text() if sandbox["log"].exists() else "")


def test_update_mode_never_prompts_links_or_reloads(sandbox):
    install(sandbox)
    before = sandbox["log"].read_text()
    proc = subprocess.run(["/bin/bash", str(sandbox["app"] / "install" / "user-setup.sh"), "--update",
                           "--unattended", "--progress", "--home", str(sandbox["data"])],
                          env=sandbox["env"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stderr
    new_calls = sandbox["log"].read_text()[len(before):]
    assert "link" not in new_calls and "launchctl" not in new_calls
    assert proc.stdout.splitlines()[-1] == "@@done"
    assert not (sandbox["data"] / ".plist-changed").exists()


def test_update_flags_a_changed_service_definition(sandbox):
    install(sandbox)
    plist = sandbox["home"] / "Library" / "LaunchAgents" / "com.infocus.editor.plist"
    plist.write_text(plist.read_text().replace("<integer>15</integer>", "<integer>99</integer>"))
    subprocess.run(["/bin/bash", str(sandbox["app"] / "install" / "user-setup.sh"), "--update",
                    "--home", str(sandbox["data"])], env=sandbox["env"], capture_output=True, check=True,
                   stdin=subprocess.DEVNULL, timeout=60)
    assert (sandbox["data"] / ".plist-changed").exists()
    assert "<integer>15</integer>" in plist.read_text()


def test_bootstrap_skips_power_settings_on_a_personal_computer():
    text = (REPO_ROOT / "install" / "bootstrap-root.sh").read_text()
    assert 'if [ "$PERSONAL" = 1 ]; then\n  log "personal computer: leaving the power settings alone"\nelse\n  "$HELPER" apply-power' in text


def test_plist_relaunches_promptly():
    import plistlib

    raw = (REPO_ROOT / "launchd" / "com.infocus.editor.plist").read_text()
    data = plistlib.loads(raw.replace("__REPO__", "/r").replace("__HOME__", "/h").replace("__UV__", "/u")
                          .replace("__DATA__", "/d").encode())
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True
    assert data["ThrottleInterval"] <= 15  # an update right after start waits at most this long
    assert data["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-dimsu"]
    assert data["EnvironmentVariables"]["INFOCUS_EDITOR_HOME"] == "/d"
