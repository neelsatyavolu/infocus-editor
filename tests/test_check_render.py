"""check_render.py's loudness rule: a segment passes only with its peak at -6.0 ±0.3 dBFS."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/infocus-show/scripts"
sys.path.insert(0, str(SCRIPTS))

import check_render  # noqa: E402


@pytest.mark.parametrize("peak,ok", [(-6.0, True), (-5.75, True), (-6.29, True), (-7.7, False),
                                     (-6.4, False), (-5.6, False), (None, False)])
def test_peak_rule(peak, ok):
    assert check_render.peak_ok(peak) is ok


def test_segment_at_minus_7_7_dbfs_fails_the_peak_check(tmp_path):
    """Two 3 s segments in one file: the first peaks at -6, the second (like the 10-21 throw) at -7.7."""
    sr, fps = 48000, 60000 / 1001
    t = np.arange(3 * sr) / sr
    tone = np.sin(2 * np.pi * 1000 * t)
    x = np.concatenate([tone * 10 ** (-6 / 20), tone * 10 ** (-7.7 / 20)])
    sf.write(tmp_path / "show.wav", np.stack([x, x], 1).astype(np.float32), sr)
    seg = round(3 * fps)
    layout = {"segments": [{"id": "open", "kind": "source", "rec_in": 0, "rec_out": seg},
                           {"id": "throw", "kind": "source", "rec_in": seg, "rec_out": 2 * seg}]}
    edl = {"fps": "60000/1001", "audio": {"peak_dbfs": -6, "dialogue_lufs": -19.5}}
    rows = {r["segment"]: r for r in check_render.loudness_rows(str(tmp_path / "show.wav"), layout, edl)}
    assert rows["open"]["ok"] is True and rows["open"]["peak"] == pytest.approx(-6.0, abs=0.2)
    assert rows["throw"]["ok"] is False and rows["throw"]["peak"] == pytest.approx(-7.7, abs=0.2)
