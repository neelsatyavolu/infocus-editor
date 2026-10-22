import json
import logging
import subprocess

import pytest

from editor.keychain import Keychain
from editor.logs import _HttpxFailuresOnly, logs_report
from editor.scrub import forget_secrets, remember_secret, scrub


@pytest.fixture(autouse=True)
def _clean_secrets():
    forget_secrets()
    yield
    forget_secrets()


@pytest.mark.parametrize("text,secret", [
    ("token sk-ant-oat01-" + "z" * 60, "z" * 20),
    ("portal ifd_live_0123456789abcdef", "0123456789abcdef"),
    ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ("GET https://drive.example.org/api/service/file?path=x&token=rw.1777777777.deadbeef&inline=false", "deadbeef"),
    ('{"token": "tok-value-1"}', "tok-value-1"),
    ('{"access_token":"at value with spaces"}', "at value with spaces"),
    ('{"refresh_token": "rt-\\"quoted\\"-value"}', "quoted"),
    ('{"id_token": "eyJx"}', "eyJx"),
    ('{"api_key": "key-value-2"}', "key-value-2"),
    ('{"password": "hunter2hunter2"}', "hunter2hunter2"),
    ("password=hunter2hunter2 next", "hunter2hunter2"),
    ('{"cmd": "echo {\\"token\\": \\"nested-secret\\"}"}', "nested-secret"),
    ('{"out": "{\\"password\\":\\"alpha beta gamma\\"}"}', "beta"),
    ("password: 12345678", "12345678"),
])
def test_scrub_masks_known_secret_shapes(text, secret):
    out = scrub(text)
    assert secret not in out, out


def test_scrubbed_json_stays_valid_json():
    line = json.dumps({"type": "item", "cmd": 'curl -H "Authorization: Bearer abc.def" "x?token=t1"',
                       "auth": {"refresh_token": "rt-1", "token": "t-2"}, "inner": '{"password": "p-3"}'})
    out = scrub(line)
    data = json.loads(out)
    assert data["auth"] == {"refresh_token": "[hidden]", "token": "[hidden]"}
    assert "abc.def" not in out and "t1" not in out and "p-3" not in out


def test_json_literals_stay_valid_json():
    line = '{"token": null, "api_key": 12345, "password": true, "out": "{\\"password\\":\\"a b\\"}"}'
    data = json.loads(scrub(line))
    assert data["token"] is None and data["api_key"] == 12345 and data["password"] is True
    assert json.loads(data["out"]) == {"password": "[hidden]"}


def test_scrub_keeps_paths_ids_and_usage_counts():
    path = "/Users/editor/InFocusEditor/jobs/2026-10-21-cjld2cyuq0000qzrmn831i8rc/show/analysis/segments/x.json"
    nas = "Package Storage/Shows/2026-10-21/editor-logs/j1-v1-20261021T221500.tar.gz"
    text = (f'session 3f2a1b4c-5d6e-7f80-9a1b-2c3d4e5f6a7b wrote {path} to {nas} '
            f'{{"input_tokens": 120, "max_tokens": 9}}')
    assert scrub(text) == text


def test_long_opaque_strings_are_masked():
    assert scrub("key " + "A1b2C3d4" * 6) == "key [hidden]"


def test_loaded_keychain_secrets_are_masked_exactly():
    values = {"editor_token": "plain-editor-token-123", "claude_oauth_token": "sk-ant-oat01-" + "q" * 50,
              "other": "not-a-secret-value"}

    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=values[cmd[cmd.index("-a") + 1]] + "\n", stderr="")

    keychain = Keychain(run=run)
    assert keychain.get("editor_token") == "plain-editor-token-123"
    keychain.get("claude_oauth_token")
    keychain.get("other")
    out = scrub("sent plain-editor-token-123 and not-a-secret-value")
    assert out == "sent [hidden] and not-a-secret-value"


def test_remembered_values_win_over_patterns():
    remember_secret("short")  # too short to be a secret on its own
    remember_secret("weird token with spaces")
    assert scrub("a short weird token with spaces b") == "a short [hidden] b"


# ---- the `logs` command ------------------------------------------------------------


def write_job_log(work_root, name, lines):
    folder = work_root / name / "logs"
    folder.mkdir(parents=True)
    (folder / "editor.log").write_text("".join(lines))
    return folder / "editor.log"


def test_logs_report_appends_the_latest_job_tail(tmp_path):
    import os

    global_log = tmp_path / "infocus-editor.log"
    global_log.write_text("".join(f"global {i}\n" for i in range(200)))
    old = write_job_log(tmp_path / "jobs", "2026-10-14-old", ["old job line\n"])
    os.utime(old, (1, 1))
    write_job_log(tmp_path / "jobs", "2026-10-21-j1", [f"job line {i}\n" for i in range(300)] +
                  ["upload token=rw.1777777777.deadbeef\n"])
    out = logs_report(global_log, tmp_path / "jobs")
    assert len(out) <= 4000
    assert "----- latest job: 2026-10-21-j1 -----" in out and "old job line" not in out
    assert out.rstrip().endswith("upload token=[hidden]") and "deadbeef" not in out
    assert "global 199" in out and "job line 299" in out
    assert "job line 100\n" not in out  # only the last 150 lines of the job log at most


def test_logs_report_keeps_the_most_recent_lines_when_trimming(tmp_path):
    global_log = tmp_path / "infocus-editor.log"
    global_log.write_text("".join(f"global {i:03d} " + "x" * 60 + "\n" for i in range(100)))
    write_job_log(tmp_path / "jobs", "2026-10-21-j1", [f"job {i:03d} " + "y" * 60 + "\n" for i in range(150)])
    out = logs_report(global_log, tmp_path / "jobs")
    assert len(out) <= 4000
    head, tail = out.split("----- latest job: 2026-10-21-j1 -----")
    assert "global 099" in head and "global 000" not in head
    assert "job 149" in tail and "job 000" not in tail
    assert len(head) > 1500 and len(tail) > 1500  # both parts get a fair share


def test_logs_report_without_a_job_is_the_global_tail(tmp_path):
    global_log = tmp_path / "infocus-editor.log"
    global_log.write_text("only line\n")
    assert logs_report(global_log, tmp_path / "jobs") == "only line\n"
    assert "No log at" in logs_report(tmp_path / "missing.log", None)


def test_httpx_success_lines_are_dropped_but_failures_kept():
    f = _HttpxFailuresOnly()

    def record(status, level=logging.INFO):
        return logging.LogRecord("httpx", level, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
                                 ("POST", "https://portal.example.org/heartbeat", "HTTP/1.1", status, "x"), None)

    assert not f.filter(record(200)) and not f.filter(record(304))
    assert f.filter(record(500)) and f.filter(record(401))
    assert f.filter(logging.LogRecord("httpx", logging.WARNING, __file__, 1, "odd", (), None))
