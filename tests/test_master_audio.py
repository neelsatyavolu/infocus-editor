"""master_audio.py: every segment (anchors, throws, packages, intro/outro) lands at a -6 dBFS true peak."""
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

SCRIPT = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts/master_audio.py"
SR = 48000


def speech_like(sec, level, rng):
    """Syllable-gated Gaussian noise: a crest factor close to real dialogue."""
    n = int(sec * SR)
    gate = (np.sin(np.arange(n) / SR * 2 * np.pi * 3) > 0).astype(float)
    gate = np.convolve(gate, np.ones(480) / 480, "same")
    return np.clip(rng.standard_normal(n), -6, 6) / 6 * gate * level


def voiced(sec, level):
    """A 150 Hz voice with harmonics, syllable-gated: clips in runs of flat samples, like a hot mic does."""
    t = np.arange(int(sec * SR)) / SR
    gate = np.convolve((np.sin(2 * np.pi * 3 * t) > 0).astype(float), np.ones(480) / 480, "same")
    tone = sum(a * np.sin(2 * np.pi * 150 * k * t + k) for k, a in [(1, 0.6), (2, 0.3), (3, 0.2), (5, 0.1)])
    return tone * gate * level


def stereo(x):
    return np.stack([x, x], 1).astype(np.float32)


def true_peak(path):
    err = subprocess.run(["ffmpeg", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    return float(re.findall(r"Peak:\s+(-?[\d.]+) dBFS", err)[-1])


@pytest.fixture(scope="module")
def mastered(tmp_path_factory):
    d = tmp_path_factory.mktemp("show")
    rng = np.random.default_rng(7)
    hot = np.clip(voiced(4, 1.6), -1, 1)                                            # mic gain far too hot: flat tops
    src = np.concatenate([speech_like(10, 0.5, rng), speech_like(6, 0.05, rng), hot])  # open, a quiet throw, a hot one
    sf.write(d / "source.wav", stereo(src), SR)
    sf.write(d / "pkg.wav", stereo(speech_like(8, 0.3, rng)), SR)
    t = np.arange(int(4 * SR)) / SR
    sf.write(d / "intro.wav", stereo(0.9 * np.sin(2 * np.pi * 440 * t)), SR)          # music near 0 dBFS
    sf.write(d / "hiss.wav", stereo(10 ** (-70 / 20) * np.sin(2 * np.pi * 440 * t)), SR)
    sf.write(d / "silent_pkg.wav", stereo(10 ** (-75 / 20) * np.sin(2 * np.pi * 300 * t)), SR)
    edl = {"show": "Test Show", "fps": "60000/1001", "source": str(d / "source.wav"),
           "segments": [
               {"id": "intro", "kind": "file", "path": str(d / "intro.wav")},
               {"id": "open", "kind": "source", "in": 0, "out": 599},
               {"id": "throw", "kind": "source", "in": 600, "out": 959},
               {"id": "hot", "kind": "source", "in": 960, "out": 1199},
               {"id": "pkg", "kind": "file", "path": str(d / "pkg.wav"), "audio": "match"},
               {"id": "hiss", "kind": "file", "path": str(d / "hiss.wav")},
               {"id": "silent_pkg", "kind": "file", "path": str(d / "silent_pkg.wav"), "audio": "match"},
           ]}
    (d / "edl.json").write_text(json.dumps(edl))
    run = subprocess.run([sys.executable, str(SCRIPT), str(d)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    levels = {s["id"]: s for s in json.loads((d / "Audio/levels.json").read_text())["segments"]}
    return d, levels, run.stdout


@pytest.mark.parametrize("seg", ["open", "intro"])
def test_anchor_and_music_segments_land_at_minus_6_true_peak(mastered, seg):
    d, levels, _ = mastered
    assert true_peak(d / f"Audio/{seg}.wav") == pytest.approx(-6.0, abs=0.15)
    assert levels[seg]["reached"] is True


def test_quiet_throw_reaches_minus_6_and_matches_the_open(mastered):
    d, levels, _ = mastered                                       # recorded 20 dB under the open
    assert abs(levels["throw"]["lufs"] - levels["open"]["lufs"]) <= 3.0
    assert true_peak(d / "Audio/throw.wav") == pytest.approx(-6.0, abs=0.15)
    assert levels["throw"]["reached"] is True


def test_match_mode_package_ends_at_minus_6_dbfs_true_peak(mastered):
    d, levels, _ = mastered
    assert true_peak(d / "Audio/pkg.wav") == pytest.approx(-6.0, abs=0.15)
    assert levels["pkg"]["reached"] is True


@pytest.mark.parametrize("seg", ["hiss", "silent_pkg"])
def test_near_silent_segment_is_reported_not_boosted_past_6_db(mastered, seg):
    d, levels, out = mastered
    assert levels[seg]["reached"] is False
    assert levels[seg]["lift_db"] <= 6.0
    assert true_peak(d / f"Audio/{seg}.wav") < -40
    assert re.search(rf"{seg}.*can't reach", out)


def test_clipped_anchor_audio_is_declipped_reported_and_still_at_minus_6(mastered):
    d, levels, out = mastered
    assert levels["hot"]["clipped_pct"] > 1.0
    assert levels["hot"]["declipped"] is True
    assert true_peak(d / "Audio/hot.wav") == pytest.approx(-6.0, abs=0.15)
    assert re.search(r"hot.*clipped.*mic gain", out)


def test_flat_tops_are_rebuilt(mastered):
    d, levels, _ = mastered
    x = sf.read(d / "Audio/hot.wav", dtype="float32")[0][:, 0]
    top = np.abs(x) >= 0.999 * np.abs(x).max()
    assert (top[1:] & top[:-1]).mean() * 100 < levels["hot"]["clipped_pct"] / 4


def test_heavy_clipping_is_rebuilt_close_to_the_original():
    """11-18-2026 (second cut): up to 20% of the mic's samples were flat. The declip has to rebuild that well, not
    just remove the flat runs: within 17 dB SNR of the unclipped voice (the old w=20 setting managed 11 dB)."""
    sys.path.insert(0, str(SCRIPT.parent))
    import master_audio
    clean = voiced(4, 1.0) / 0.45
    clipped = stereo(np.clip(clean, -1, 1))
    assert master_audio.clipped_pct(clipped) > 20
    rebuilt = master_audio.filt(clipped, master_audio.DECLIP)[: len(clean), 0]
    ref = clean[: len(rebuilt)]
    err = ref - (ref @ rebuilt) / (rebuilt @ rebuilt) * rebuilt
    assert 10 * np.log10((ref @ ref) / (err @ err)) >= 17


def test_clean_anchor_audio_is_left_alone(mastered):
    _, levels, _ = mastered
    for seg in ("open", "throw"):
        assert levels[seg]["clipped_pct"] < 0.05
        assert levels[seg]["declipped"] is False
