import json
import logging

from editor.joblog import Transcript, clip, describe_claim, describe_payload, describe_recordings, job_log
from editor.portal import Claim, Download, Fix


def test_job_log_writes_debug_lines_with_a_run_header_and_detaches(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    with job_log(tmp_path, "edit", 1):
        logging.getLogger("editor.runner").debug("step one")
        logging.getLogger("editor.sessions").info("step two sk-ant-oat01-%s", "s" * 50)
        logging.getLogger("httpx").debug("library debug stays out")
    logging.getLogger("editor.runner").info("after the job")
    assert root.handlers == before
    assert logging.getLogger("editor").level == logging.NOTSET
    text = (tmp_path / "logs" / "editor.log").read_text()
    lines = text.splitlines()
    assert lines[0] == "----- run 1 (edit, v1) -----"
    assert "DEBUG   editor.runner: step one" in lines[1] and lines[1][:4] == "20" + lines[1][2:4]
    assert "INFO    editor.sessions: step two sk-ant-[hidden]" in lines[2]
    assert "library debug" not in text and "after the job" not in text


def test_reruns_and_fixes_append_numbered_run_headers(tmp_path):
    for kind, version in (("edit", 1), ("edit", 1), ("fix", 2)):
        with job_log(tmp_path, kind, version):
            logging.getLogger("editor.runner").info("%s v%s", kind, version)
    headers = [line for line in (tmp_path / "logs" / "editor.log").read_text().splitlines()
               if line.startswith("-----")]
    assert headers == ["----- run 1 (edit, v1) -----", "----- run 2 (edit, v1) -----",
                       "----- run 3 (fix, v2) -----"]


def test_global_handlers_keep_their_level_during_a_job(tmp_path):
    seen = []

    class Collect(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = Collect()
    root = logging.getLogger()
    root.addHandler(handler)
    old = root.level
    root.setLevel(logging.INFO)
    try:
        with job_log(tmp_path, "edit", 1):
            logging.getLogger("editor.runner").debug("job-only detail")
            logging.getLogger("editor.runner").info("worth the global log")
        assert handler.level == logging.NOTSET
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
    assert seen == ["worth the global log"]


def test_an_unwritable_job_folder_never_breaks_the_job(tmp_path):
    blocker = tmp_path / "job"
    blocker.write_text("a file where the folder should be")
    with job_log(blocker, "edit", 1):
        logging.getLogger("editor.runner").info("still runs")
    t = Transcript(blocker, "codex")
    t.line('{"type": "x"}')
    t.stderr("oops")
    t.close()
    assert t.path is None


def test_transcripts_are_numbered_per_job_folder_and_scrubbed(tmp_path):
    with Transcript(tmp_path, "claude") as one:
        one.event("AssistantMessage", {"content": [{"text": "hi"}], "auth": {"token": "t-1"}})
    with Transcript(tmp_path, "codex") as two:
        two.line('{"type": "thread.started", "thread_id": "t"}')
        two.stderr("warning: Bearer abc.def")
    assert one.path.name == "agent-01-claude.jsonl" and two.path.name == "agent-02-codex.jsonl"
    assert json.loads(one.path.read_text()) == {"type": "AssistantMessage", "content": [{"text": "hi"}],
                                                "auth": {"token": "[hidden]"}}
    assert (tmp_path / "logs" / "agent-02-codex.stderr.log").read_text() == "warning: Bearer [hidden]\n"


def test_clip_makes_one_short_line():
    assert clip("a\n  b\tc") == "a b c"
    assert len(clip("x" * 1000)) == 300 and clip("x" * 1000).endswith("…")
    assert clip({"path": "a"}) == '{"path": "a"}'


def test_summaries_never_include_urls_or_tokens():
    claim = Claim("fix", "j1", "2026-10-21", 2,
                  {"sources": [{"path": "a", "role": "auto"}, {"path": "b", "role": "ignore"}],
                   "packages": [{"nasPath": "p"}], "filmedOn": "2026-10-20"},
                  (Download("a", "https://drive.example.org/f?token=rw.1777777777.secret", 10),),
                  Fix("Make the card say Tomorrow", True, "claude:s1", 2))
    text = describe_claim(claim)
    assert "secret" not in text and "drive.example.org" not in text
    assert "job j1, show 2026-10-21, fix v2, 2 source(s) (1 ignored), 1 package(s), 1 download(s)" in text
    assert "session claude:s1" in text and "Make the card say Tomorrow" in text
    lines = describe_recordings({"recordings": [{"id": "r1", "kind": "program", "frames": 100, "confidence": 1.0,
                                                 "files": [{"path": "/j/src/01.mov", "role": "program",
                                                            "offsetFrames": 0}]}],
                                 "skipped": [{"name": "sd.mov", "reason": "SD capture"}]})
    assert lines[0] == "recording r1: program, 100 frames, confidence 1.0, files 01.mov=program@0"
    assert "SD capture" in lines[1]
    summary = describe_payload({"needsReview": True, "checks": {"coversClean": False,
                                                                "failures": [{"rule": "audio"}]},
                                "decisions": [{"flagged": True}, {"flagged": False}], "costUsd": 1.5,
                                "agentSessionId": "claude:s1", "outputNasPath": "out.mp4"})
    assert "needsReview=True" in summary and "['audio']" in summary and "2 decision(s) (1 flagged)" in summary
