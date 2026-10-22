import json
import os
import tarfile

from editor.debugbundle import MAX_FILE_BYTES, build_bundle, bundle_path, truncate_middle


def make_job(tmp_path):
    job = tmp_path / "2026-10-21-j1"
    files = {
        "logs/editor.log": "----- run 1 (edit, v1) -----\n",
        "logs/agent-01-claude.jsonl": '{"type": "x"}\n',
        "logs/agent-02-codex.stderr.log": "warn\n",
        "logs/debug-0.tar.gz": "an older bundle",
        "brief.json": "{}", "result.json": "{}", "status.json": "{}",
        "house-rules.md": "not in the bundle",
        "show/edl.json": "{}", "show/recordings.json": "{}", "show/layout.json": "{}",
        "show/check/verify.json": '{"pass": true}',
        "show/analysis/report.md": "# report",
        "show/analysis/seg/report.md": "# nested report",
        "show/analysis/seg/takes.json": "[]",
        "show/analysis/seg/notes.txt": "not listed",
        "show/analysis/seg/frame.png": "media",
        "show/Graphics/card.mov": "media",
        "src/01.mov": "media",
        "out/InFocus Show 2026-10-21 v1.mp4": "media",
        "logs/still.jpg": "media",
    }
    for rel, text in files.items():
        path = job / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    big = job / "show" / "analysis" / "big.json"
    with big.open("wb") as fh:
        fh.truncate(MAX_FILE_BYTES + 1)
    return job


def names(bundle):
    with tarfile.open(bundle) as tar:
        return sorted(tar.getnames())


def test_bundle_has_logs_and_small_json_but_no_media(tmp_path):
    job = make_job(tmp_path)
    bundle = build_bundle(job, 1)
    assert bundle == bundle_path(job, 1) == job / "logs" / "debug-1.tar.gz"
    assert names(bundle) == sorted([
        "logs/editor.log", "logs/agent-01-claude.jsonl", "logs/agent-02-codex.stderr.log",
        "brief.json", "result.json", "status.json",
        "show/edl.json", "show/recordings.json", "show/layout.json", "show/check/verify.json",
        "show/analysis/report.md", "show/analysis/seg/report.md", "show/analysis/seg/takes.json",
    ])


def test_bundled_job_files_are_scrubbed_and_the_originals_left_alone(tmp_path):
    job = make_job(tmp_path)
    (job / "brief.json").write_text('{"note": "use token=abc123 please", "auth": {"password": "p w"}}')
    with tarfile.open(build_bundle(job, 1)) as tar:
        brief = json.loads(tar.extractfile("brief.json").read())
    assert brief == {"note": "use token=[hidden] please", "auth": {"password": "[hidden]"}}
    assert "abc123" in (job / "brief.json").read_text()


def test_rebuilding_never_nests_an_older_bundle(tmp_path):
    job = make_job(tmp_path)
    build_bundle(job, 1)
    assert not any(n.endswith(".tar.gz") for n in names(build_bundle(job, 2)))


def test_over_the_cap_the_transcripts_lose_their_middle(tmp_path):
    job = make_job(tmp_path)
    transcript = job / "logs" / "agent-01-claude.jsonl"
    with transcript.open("w") as fh:
        for i in range(4000):
            noise = " ".join(os.urandom(16).hex() for _ in range(12))  # short words: nothing to scrub
            fh.write(json.dumps({"n": i, "noise": noise}) + "\n")
    cap = 600 * 1024
    bundle = build_bundle(job, 1, cap_bytes=cap)
    assert bundle.stat().st_size <= cap
    with tarfile.open(bundle) as tar:
        lines = tar.extractfile("logs/agent-01-claude.jsonl").read().decode().splitlines()
    events = [json.loads(line) for line in lines]  # still one JSON object per line
    marker = [e for e in events if e.get("type") == "truncated"]
    assert len(marker) == 1 and "cut here to keep the debug bundle under 50 MB" in marker[0]["note"]
    assert events[0]["n"] == 0 and events[-1]["n"] == 3999
    assert transcript.stat().st_size > cap  # the file on disk is left whole


def test_truncate_middle_keeps_whole_lines():
    data = b"".join(f"line {i}\n".encode() for i in range(100))
    out = truncate_middle(data, 100, jsonl=False).decode().splitlines()
    assert out[0] == "line 0" and out[-1] == "line 99" and out[len(out) // 2].startswith("-----")
    assert truncate_middle(b"short\n", 100, jsonl=True) == b"short\n"
