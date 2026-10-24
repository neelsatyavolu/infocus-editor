"""prevent-shutdown.sh drops a shutdown schedule and puts the weekday wake back."""
import os
import subprocess

from editor.config import REPO_ROOT

SCRIPT = REPO_ROOT / "install" / "prevent-shutdown.sh"


def run(tmp_path, sched: str) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "pmset.log"
    fake = bindir / "pmset"
    fake.write_text(
        "#!/bin/bash\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        'if [ "$1" = "-g" ]; then\n'
        f"  printf '%s\\n' {sched!r}\n"
        "fi\n"
    )
    fake.chmod(0o755)
    env = os.environ.copy()
    env["INFOCUS_PMSET"] = str(fake)
    out = subprocess.run(["/bin/bash", str(SCRIPT)], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    return log.read_text() if log.exists() else ""


def test_clears_shutdown_and_restores_weekday_wake(tmp_path):
    sched = "Repeating power events:\\n  shutdown at 6:00PM weekdays only\\n  wakepoweron at 7:00AM weekdays only"
    log = run(tmp_path, sched)
    assert "repeat cancel" in log
    assert "repeat wakeorpoweron MTWRF 07:00:00" in log


def test_leaves_a_schedule_with_no_shutdown_alone(tmp_path):
    log = run(tmp_path, "Repeating power events:\\n  wakepoweron at 7:00AM weekdays only")
    assert "repeat cancel" not in log


def test_cancels_a_one_shot_shutdown(tmp_path):
    sched = "Scheduled power events:\\n [0]  shutdown at 10/23/2026 19:40:00"
    log = run(tmp_path, sched)
    assert 'schedule cancel shutdown 10/23/2026 19:40:00' in log
    assert "repeat wakeorpoweron MTWRF 07:00:00" in log
