from editor.config import REPO_ROOT
from editor.doctor import NO_BACKEND_PROBLEM, VIDEOTOOLBOX_WARNING, Probes, check, helper_version
from tests.helpers import make_config

AC = "Now drawing from 'AC Power'\n"


def probes(**kw):
    base = dict(
        which=lambda tool: f"/usr/bin/{tool}",
        exists=lambda path: True,
        free_bytes=lambda path: 500 * 1024**3,
        power_source=lambda: AC,
        playwright_ok=lambda: True,
        admin_step_ok=lambda repo: True,
        ffmpeg_encoders=lambda: " V....D h264_videotoolbox    VideoToolbox H.264 Encoder\n",
        connected_backends=lambda: ["claude"],
    )
    base.update(kw)
    return Probes(**base)


def test_healthy_mac_has_no_problems(tmp_path):
    health = check(make_config(tmp_path), probes())
    assert health.ok and health.problems == ()


def test_no_connected_ai_account_blocks(tmp_path):
    health = check(make_config(tmp_path), probes(connected_backends=lambda: []))
    assert not health.ok and health.problems == (NO_BACKEND_PROBLEM,)
    assert NO_BACKEND_PROBLEM == "No AI account connected: in the Portal open Automatic editing → Connect AI account."


def test_missing_videotoolbox_is_only_a_warning(tmp_path):
    health = check(make_config(tmp_path), probes(ffmpeg_encoders=lambda: " V....D libx264  H.264\n"))
    assert health.ok and health.problems == (VIDEOTOOLBOX_WARNING,)


def test_doctor_reports_each_missing_tool_as_one_problem(tmp_path):
    health = check(make_config(tmp_path), probes(which=lambda t: None if t in ("ffmpeg", "node") else "/x"))
    assert not health.ok
    assert [p for p in health.problems if p.startswith("ffmpeg is missing")] == [
        "ffmpeg is missing: run `brew install ffmpeg`."]
    assert sum(p.startswith("node is missing") for p in health.problems) == 1
    assert len(health.problems) == 2


def test_battery_power_is_a_problem(tmp_path):
    health = check(make_config(tmp_path), probes(power_source=lambda: "Now drawing from 'Battery Power'"))
    assert health.problems == ("The MacBook is on battery; plug it in.",)


def test_low_disk(tmp_path):
    health = check(make_config(tmp_path), probes(free_bytes=lambda p: 20 * 1024**3))
    assert any("20 GB free" in p for p in health.problems)


def test_missing_package_intro(tmp_path):
    health = check(make_config(tmp_path), probes(exists=lambda p: not str(p).endswith("InFocus Intro 2026.mp4")))
    assert health.problems == ("The package is missing 03 Intro & Outro/InFocus Intro 2026.mp4.",)


def test_missing_admin_step_is_reported_first(tmp_path):
    health = check(make_config(tmp_path), probes(admin_step_ok=lambda repo: False))
    assert health.problems == ("The one-time admin step is missing or out of date. "
                               "Run install/install.sh; an admin needs to approve once.",)


def test_no_resolve_checks_remain(tmp_path):
    health = check(make_config(tmp_path), probes(exists=lambda p: "DaVinci" not in str(p)))
    assert health.ok


def test_helper_version_reads_the_repo_copy():
    assert helper_version(REPO_ROOT / "install" / "infocus-root") == "1"
