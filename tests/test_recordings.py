"""Detecting what a show's files are from their content (editor/recordings.py, docs/multicam.md)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from editor.jobfiles import JobPaths
from editor.portal import Claim, Download
from editor.prepare import JobFailed, prepare_edit
from editor.recordings import Candidate, DetectionError, Detector, Profile, structure_summary
from editor.sources import parse_sources
from editor.sync import SAMPLE_RATE, envelope, read_audio

FRAME = 1001 / 60000
FPS = 60000 / 1001


def speechy(seconds: float, seed: int) -> np.ndarray:
    """Noise with a syllable-like loudness pattern, so the envelope has structure."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SAMPLE_RATE)
    bursts = np.repeat(rng.random(int(seconds * 8)) ** 3, SAMPLE_RATE // 8)[:n]
    return rng.standard_normal(n) * np.pad(bursts, (0, max(0, n - len(bursts))))


def clip(base: np.ndarray, start: float, length: float) -> np.ndarray:
    return base[int(start * SAMPLE_RATE):int((start + length) * SAMPLE_RATE)]


class Files:
    """Synthetic files: real bytes on disk (for duplicate detection), fake probe, audio and picture."""

    def __init__(self, root: Path):
        self.root = root
        self.probes, self.audio, self.pictures = {}, {}, {}
        root.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def add(self, *, audio=None, seconds=50.0, picture=("M",), width=1920, height=1080, content=None,
            fps="60000/1001", role="auto", mtime=None, name=None):
        self.n += 1
        path = self.root / f"{self.n:02d}.mov"
        path.write_bytes(content if content is not None else f"file {self.n}".encode() * 20)
        self.probes[path.name] = {"format": {"duration": str(seconds)}, "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": width, "height": height, "r_frame_rate": fps},
            *([{"codec_type": "audio", "codec_name": "pcm_s16le"}] if audio is not None else [])]}
        self.audio[path.name] = audio
        self.pictures[path.name] = picture
        return Candidate(path, name or f"InFocus Show 08-26-2026 ({self.n}).mov", role, self.n, mtime)

    def run(self, cmd, **kw):
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.probes[Path(cmd[-1]).name]), "")
        Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(cmd[-1]).write_bytes(b"conformed")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def detector(self, show: Path) -> Detector:
        profiled = []
        self.profiled = profiled

        def profile(path):
            profiled.append(path.name)
            return Profile(tuple(self.pictures[path.name]))

        return Detector(show, run=self.run, profile=profile,
                        envelope=lambda p: None if self.audio[p.name] is None else envelope(self.audio[p.name]))


def files_of(rec):
    return [(Path(f["path"]).name, f["role"], f["offsetFrames"]) for f in rec["files"]]


# ---- grouping and offsets -----------------------------------------------------------------


def test_three_feeds_with_known_offsets_are_recovered_within_a_frame(tmp_path):
    base = speechy(70, seed=1)
    fs = Files(tmp_path / "src")
    m = fs.add(audio=clip(base, 2.0, 50), picture=["M"] * 25)
    left = fs.add(audio=clip(base, 0.5, 50), picture=["L"] * 25)
    right = fs.add(audio=clip(base, 3.25, 50), picture=["R"] * 25)
    found = fs.detector(tmp_path / "show").detect([m, left, right])
    (rec,) = found.data["recordings"]
    assert rec["kind"] == "multicam" and rec["id"] == "r1"
    # Common frame 0 = base time 3.25 (R starts last); each offset = (3.25 - start) in that file.
    want = {"01.mov": ("M", 1.25), "02.mov": ("L", 2.75), "03.mov": ("R", 0.0)}
    for name, role, offset in files_of(rec):
        assert role == want[name][0]
        assert abs(offset - want[name][1] / FRAME) <= 1, (name, offset)
    assert abs(rec["frames"] - (50.5 - 3.25) * FPS) <= 2  # until L ends (base 0.5 + 50)
    assert rec["confidence"] > 0.8 and not found.needs_review and found.source is None
    assert json.loads((tmp_path / "show" / "recordings.json").read_text()) == found.data


def test_two_recordings_of_three_cameras_are_grouped_and_ordered_by_export_time(tmp_path):
    first, second = speechy(60, seed=11), speechy(60, seed=22)
    fs = Files(tmp_path / "src")
    cands = []
    # Uploaded interleaved and out of order: the second recording finished exporting first? No: by mtime.
    for base, t0, mtime in ((second, 0.0, 2000.0), (first, 0.0, 1000.0)):
        for cam, start in (("M", 1.0), ("L", 0.0), ("R", 2.0)):
            cands.append(fs.add(audio=clip(base, start, 50), picture=[cam] * 25, mtime=mtime + start))
    found = fs.detector(tmp_path / "show").detect(cands)
    recs = found.data["recordings"]
    assert [r["id"] for r in recs] == ["r1", "r2"] and all(r["kind"] == "multicam" for r in recs)
    assert [sorted(Path(f["path"]).name for f in r["files"]) for r in recs] == [
        ["04.mov", "05.mov", "06.mov"], ["01.mov", "02.mov", "03.mov"]]  # r1 = the earlier export
    assert [[f["role"] for f in r["files"]] for r in recs] == [["M", "L", "R"], ["M", "L", "R"]]
    assert found.decisions[-1]["detail"] == "2 recordings: r1 multicam (M, L, R), r2 multicam (M, L, R)"


def test_separate_takes_are_separate_program_recordings(tmp_path):
    fs = Files(tmp_path / "src")
    a = fs.add(audio=speechy(151, seed=5), seconds=151, picture=["M", "L", "FS"] * 20)
    b = fs.add(audio=speechy(169, seed=6), seconds=169, picture=["M", "R"] * 20)
    found = fs.detector(tmp_path / "show").detect([a, b])
    assert [(r["kind"], files_of(r)) for r in found.data["recordings"]] == [
        ("program", [("01.mov", "program", 0)]), ("program", [("02.mov", "program", 0)])]
    assert fs.profiled == []  # single-file recordings need no picture profile
    assert found.decisions[-1]["detail"] == "2 recordings: r1 program, r2 program"


def test_weak_match_is_grouped_but_flagged(tmp_path):
    base = speechy(60, seed=1)
    fs = Files(tmp_path / "src")
    m = fs.add(audio=clip(base, 2.0, 50), picture=["M"] * 25)
    noisy = 0.5 * clip(base, 1.0, 50) + speechy(50, seed=123)  # peak ratio about 2.6
    left = fs.add(audio=noisy, picture=["L"] * 25)
    found = fs.detector(tmp_path / "show").detect([m, left])
    (rec,) = found.data["recordings"]
    assert rec["kind"] == "multicam" and found.needs_review
    assert any(d["kind"] == "grouping" and d["flagged"] and "weakly" in d["detail"] for d in found.decisions)


def test_ambiguous_match_stays_separate_and_flagged(tmp_path):
    base = speechy(60, seed=1)
    fs = Files(tmp_path / "src")
    a = fs.add(audio=clip(base, 2.0, 50))
    b = fs.add(audio=0.35 * clip(base, 1.0, 50) + speechy(50, seed=123))  # ratio about 1.9
    found = fs.detector(tmp_path / "show").detect([a, b])
    assert len(found.data["recordings"]) == 2 and found.needs_review
    assert any("might be the same recording" in d["detail"] for d in found.decisions)


# ---- filtering ---------------------------------------------------------------------------


def test_duplicates_and_sd_files_are_skipped(tmp_path):
    fs = Files(tmp_path / "src")
    a = fs.add(audio=speechy(120, seed=1), seconds=120, content=b"same bytes" * 50)
    dup = fs.add(audio=speechy(120, seed=1), seconds=120, content=b"same bytes" * 50)
    sd = fs.add(audio=speechy(120, seed=2), seconds=120, width=720, height=480)
    found = fs.detector(tmp_path / "show").detect([a, dup, sd])
    assert len(found.data["recordings"]) == 1 and found.source == a.local
    assert found.data["skipped"] == [
        {"path": str(dup.local), "reason": f"duplicate of src/01.mov ({a.name})"},
        {"path": str(sd.local), "reason": "SD capture (720x480)"}]
    flags = {d["detail"].split(":")[0]: d["flagged"] for d in found.decisions if d["kind"] == "skip"}
    assert flags == {f"Skipped {dup.name}": False, f"Skipped {sd.name}": True} and found.needs_review


def test_only_an_sd_file_gets_the_sd_message(tmp_path):
    fs = Files(tmp_path / "src")
    sd = fs.add(audio=speechy(30, seed=2), width=720, height=480)
    with pytest.raises(DetectionError, match="This is the SD capture; pick the HD recording."):
        fs.detector(tmp_path / "show").detect([sd])


def test_a_59_94_mismatch_is_converted(tmp_path):
    fs = Files(tmp_path / "src")
    a = fs.add(audio=speechy(80, seed=1), seconds=80, fps="30000/1001")
    found = fs.detector(tmp_path / "show").detect([a])
    assert found.data["recordings"][0]["files"][0]["path"] == str(tmp_path / "show" / "conformed" / "01.mov")


# ---- roles ------------------------------------------------------------------------------------


def test_program_feed_is_detected_inside_an_iso_recording(tmp_path):
    base = speechy(60, seed=3)
    fs = Files(tmp_path / "src")
    program = fs.add(audio=clip(base, 1.0, 50), picture=["M"] * 6 + ["L"] * 6 + ["FS"] * 2 + ["R"] * 6 + ["M"] * 5)
    m = fs.add(audio=clip(base, 1.0, 50), picture=["M"] * 25)
    left = fs.add(audio=clip(base, 0.0, 50), picture=["L"] * 23 + ["OTHER"] * 2)
    right = fs.add(audio=clip(base, 2.0, 50), picture=["R"] * 25)
    found = fs.detector(tmp_path / "show").detect([program, m, left, right])
    (rec,) = found.data["recordings"]
    assert [f[:2] for f in files_of(rec)] == [("01.mov", "program"), ("02.mov", "M"), ("03.mov", "L"), ("04.mov", "R")]
    assert rec["kind"] == "multicam" and structure_summary([rec]) == "1 recording: r1 multicam (M, L, R + program)"


def test_two_feeds_claiming_the_same_camera_are_resolved_by_share_and_flagged(tmp_path):
    base = speechy(60, seed=4)
    fs = Files(tmp_path / "src")
    a = fs.add(audio=clip(base, 1.0, 50), picture=["M"] * 24 + ["L"])
    b = fs.add(audio=clip(base, 0.0, 50), picture=["M"] * 22 + ["L"] * 3)  # also mostly M (two clean feeds)
    found = fs.detector(tmp_path / "show").detect([a, b])
    roles = {name: role for name, role, _ in files_of(found.data["recordings"][0])}
    assert roles == {"01.mov": "M", "02.mov": "L"} and found.needs_review
    assert any("looked most like camera M" in d["detail"] for d in found.decisions)


def test_portal_roles_override_detection_and_ignore_drops(tmp_path):
    base = speechy(60, seed=5)
    fs = Files(tmp_path / "src")
    a = fs.add(audio=clip(base, 1.0, 50), picture=["M"] * 25)
    b = fs.add(audio=clip(base, 0.0, 50), picture=["OTHER"] * 25, role="R")  # the picture can't say; the producer can
    c = fs.add(audio=speechy(80, seed=9), role="ignore")
    found = fs.detector(tmp_path / "show").detect([a, b, c])
    assert [f[:2] for f in files_of(found.data["recordings"][0])] == [("01.mov", "M"), ("02.mov", "R")]
    assert found.data["skipped"][0]["reason"] == "set to Ignore in the Portal"


def test_portal_side_cameras_the_picture_clearly_contradicts_follow_the_picture(tmp_path):
    base = speechy(60, seed=6)
    fs = Files(tmp_path / "src")
    m = fs.add(audio=clip(base, 1.0, 50), picture=["M"] * 25, role="M")
    shows_l = fs.add(audio=clip(base, 0.0, 50), picture=["L"] * 24 + ["M"], role="R")   # L and R swapped in the Portal
    shows_r = fs.add(audio=clip(base, 0.5, 50), picture=["R"] * 25, role="L")
    found = fs.detector(tmp_path / "show").detect([m, shows_l, shows_r])
    roles = {name: role for name, role, _ in files_of(found.data["recordings"][0])}
    assert roles == {"01.mov": "M", "02.mov": "L", "03.mov": "R"}
    assert not found.needs_review                     # verify rule 8 still checks every side take
    detail = next(d["detail"] for d in found.decisions if "02.mov" in d["detail"] or "(2)" in d["detail"])
    assert "set to R in the Portal" in detail and "used it as L" in detail


def test_a_portal_role_the_picture_only_leans_against_stays(tmp_path):
    base = speechy(60, seed=7)
    fs = Files(tmp_path / "src")
    a = fs.add(audio=clip(base, 1.0, 50), picture=["M"] * 25)
    b = fs.add(audio=clip(base, 0.0, 50), picture=["L"] * 15 + ["R"] * 10, role="R")   # R on 40%: not contradicted
    found = fs.detector(tmp_path / "show").detect([a, b])
    assert dict((n, r) for n, r, _ in files_of(found.data["recordings"][0]))["02.mov"] == "R"


def test_legacy_source_paths_mean_auto():
    sources = parse_sources({"sourcePaths": ["Shows/a.mov", "Shows/b.mov"]})
    assert [(s.path, s.role) for s in sources] == [("Shows/a.mov", "auto"), ("Shows/b.mov", "auto")]
    sources = parse_sources({"sources": [{"path": "Shows/x.mov", "role": "M", "order": 0,
                                          "modifiedAt": "2026-10-22T15:00:00Z"}]})
    assert sources[0].role == "M" and sources[0].modified_at is not None


# ---- through prepare_edit -----------------------------------------------------------------------


class Drive:
    def __init__(self, contents):
        self.contents = contents

    async def download(self, url, dest, size):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.contents[url])
        return dest


async def test_prepare_writes_recordings_and_keeps_source_only_for_a_single_program_file(tmp_path):
    fs = Files(tmp_path / "ignored")
    probes = {"01.mov": 151, "02.mov": 169}

    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"format": {"duration": str(probes[Path(cmd[-1]).name])},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                         "r_frame_rate": "60000/1001"}]}), "")

    brief = {"sources": [{"path": "Shows/InFocus Show 10-22-2026.mov", "role": "auto", "order": 0},
                         {"path": "Shows/InFocus Show 10-22-2026 (1).mov", "role": "auto", "order": 1}],
             "packages": []}
    contents = {"u0": b"take one" * 10, "u1": b"take two" * 10}
    claim = Claim("edit", "j1", "2026-10-22", 1, brief, (Download("Shows/a.mov", "u0", 80), Download("Shows/b.mov", "u1", 80)))
    paths = JobPaths(tmp_path / "job")
    prepared = await prepare_edit(Drive(contents), claim, paths, run, envelope=lambda p: None,
                                  profile=lambda p: Profile(()))
    assert prepared.source is None and prepared.recordings == paths.show / "recordings.json"
    assert len(json.loads(prepared.recordings.read_text())["recordings"]) == 2
    del fs


async def test_total_length_is_checked_across_recordings(tmp_path):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"format": {"duration": "25"},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                         "r_frame_rate": "60000/1001"}]}), "")

    brief = {"sourcePaths": ["Shows/a.mov", "Shows/b.mov"], "packages": []}
    claim = Claim("edit", "j1", "2026-10-22", 1, brief, (Download("Shows/a.mov", "u0", 10), Download("Shows/b.mov", "u1", 10)))
    with pytest.raises(JobFailed, match=r"Together, the recordings .*0:50"):
        await prepare_edit(Drive({"u0": b"a" * 10, "u1": b"b" * 10}), claim, JobPaths(tmp_path / "job"), run,
                           envelope=lambda p: None, profile=lambda p: Profile(()))


# ---- real files ------------------------------------------------------------------------------

FIXTURES = Path.home() / "InFocusEditor" / "fixtures"


@pytest.mark.fixtures
def test_real_10_22_takes_are_two_program_recordings(tmp_path):
    files = sorted((FIXTURES / "2026-10-22").glob("*.mov"))
    if len(files) < 2 or shutil.which("ffmpeg") is None:
        pytest.skip("the 10-22 fixture files aren't downloaded yet")
    cands = [Candidate(p, p.name, "auto", i) for i, p in enumerate(files)]
    found = Detector(tmp_path / "show").detect(cands)
    recs = found.data["recordings"]
    assert [r["kind"] for r in recs] == ["program", "program"]
    assert all(len(r["files"]) == 1 for r in recs)
    assert found.decisions[-1]["detail"] == "2 recordings: r1 program, r2 program"
    assert not any(d["kind"] == "grouping" for d in found.decisions)


@pytest.mark.fixtures
def test_pseudo_iso_10_21_is_one_multicam_recording(tmp_path):
    folder = FIXTURES / "2026-10-21-iso"
    package = os.environ.get("INFOCUS_PACKAGE")
    files = sorted(folder.glob("*.mov")) if folder.is_dir() else []
    if len(files) < 3 or not package:
        pytest.skip("the pseudo-ISO 10-21 fixture (and INFOCUS_PACKAGE) aren't available yet")
    from editor.config import REPO_ROOT
    from editor.recordings import profile_with_skill

    detector = Detector(tmp_path / "show", profile=profile_with_skill(
        REPO_ROOT / ".claude" / "skills" / "infocus-show" / "scripts", Path(package)))
    found = detector.detect([Candidate(p, p.name, "auto", i) for i, p in enumerate(files)])
    roles = sorted(f["role"] for r in found.data["recordings"] for f in r["files"])
    assert {"M", "L", "R"} <= set(roles)
    assert all(r["kind"] == "multicam" for r in found.data["recordings"])


def test_read_audio_with_real_ffmpeg(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    wav = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-ar", "48000", str(wav)], check=True)
    samples = read_audio(wav)
    assert samples is not None and abs(len(samples) - 2 * SAMPLE_RATE) < 200


async def test_ignored_files_are_not_in_downloads_and_are_reported(tmp_path):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"format": {"duration": "151"},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                         "r_frame_rate": "60000/1001"}]}), "")

    brief = {"sources": [{"path": "Shows/InFocus Show EDITED.mp4", "role": "ignore", "order": 0},
                         {"path": "Shows/InFocus Show 10-22-2026.mov", "role": "auto", "order": 1}],
             "packages": []}
    claim = Claim("edit", "j1", "2026-10-22", 1, brief, (Download("Shows/InFocus Show 10-22-2026.mov", "u1", 10),))
    paths = JobPaths(tmp_path / "job")
    prepared = await prepare_edit(Drive({"u1": b"t" * 10}), claim, paths, run, envelope=lambda p: None,
                                  profile=lambda p: Profile(()))
    data = json.loads(prepared.recordings.read_text())
    assert data["skipped"] == [{"path": "Shows/InFocus Show EDITED.mp4", "reason": "set to Ignore in the Portal"}]
    assert prepared.source == paths.src / "01.mov"
    assert [d["kind"] for d in prepared.decisions] == ["skip", "structure"]
