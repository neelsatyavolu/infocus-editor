"""Native AppKit checks; send Quit to our disposable helper, never log out the Mac."""
import os
import select
import shutil
import subprocess
import sys
import time

import pytest

from editor.config import REPO_ROOT
from editor.logout_guard import GUARD_PATH, guard_available

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("swiftc"),
                                reason="Requires macOS AppKit and Swift")


@pytest.fixture(scope="module")
def native_guard(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-guard")
    binary = root / GUARD_PATH
    binary.parent.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "install/logout-guard.plist", binary.parent.parent / "Info.plist")
    sender = root / "quit-guard"
    for source, output in [(REPO_ROOT / "install/logout-guard.swift", binary),
                           (REPO_ROOT / "tests/quit-guard.swift", sender)]:
        subprocess.run(["swiftc", "-module-cache-path", str(root / "modules"), str(source), "-o", str(output)],
                       check=True, capture_output=True, text=True, timeout=120)
    return binary, sender


def wait_ready(proc):
    readable, _, _ = select.select([proc.stdout], [], [], 15)
    assert readable and proc.stdout.readline() == b"ready\n", "AppKit guard did not become ready"


def test_appkit_rejects_quit_but_service_stop_works(native_guard):
    binary, sender = native_guard
    proc = subprocess.Popen([str(binary), str(os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    try:
        wait_ready(proc)
        subprocess.run([str(sender), str(proc.pid)], check=True, capture_output=True, text=True, timeout=10)
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        proc.stdout.close()


def test_health_check_launches_appkit_and_exits(native_guard):
    binary, _ = native_guard
    assert guard_available(binary.parents[3])


def test_guard_does_not_adopt_launchd_as_its_owner(native_guard):
    binary, _ = native_guard
    result = subprocess.run([str(binary), "1"], capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stdout == b""


def test_guard_exits_when_parent_dies(native_guard):
    binary, _ = native_guard
    # The intermediary waits for AppKit readiness, then dies without cleaning up its child.
    parent = subprocess.run([sys.executable, "-c", """
import os, select, subprocess, sys
p = subprocess.Popen([sys.argv[1], str(os.getpid())], stdout=subprocess.PIPE)
assert select.select([p.stdout], [], [], 15)[0]
assert p.stdout.readline() == b'ready\\n'
print(p.pid, flush=True)
os._exit(0)
""", str(binary)], capture_output=True, text=True, timeout=20)
    assert parent.returncode == 0, parent.stderr
    pid = int(parent.stdout.strip())
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        pytest.fail("Guard survived its worker")
    finally:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
