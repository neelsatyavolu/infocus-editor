"""Masters the program audio so the whole show sits at one level: peaks at -6 dBFS, same loudness everywhere.

build_ffmpeg.py puts these finished WAVs on the program audio, cut to picture. Per program segment, written to <show_dir>/Audio/<id>.wav:
  - anchor segments (source): dynamic normalization (evens out quiet vs loud mics, even within a two-shot,
    without pumping room noise), a trim so every segment has the same integrated loudness, then a limiter
    at the peak ceiling so plosives/laughs never pass it
  - packages (file segments with "audio": "match"): matched to the anchors' loudness, then the same limiter
  - intro / outro music (other file segments): one static gain
Then every segment, whatever its kind, gets one last static gain so its true peak (4x oversampled) sits exactly at
the ceiling. A throw or package whose loudest moment is under the ceiling is raised (at most +6 dB); one that
can't get there within +6 dB (near silence) is reported in the output and levels.json ("reached": false), never forced.
Digital near-silence (under -60 LUFS) isn't loudness-trimmed at all.
Each file is exactly as long as the segment's picture on the timeline.

    uv run --with numpy python master_audio.py <show_dir>
edl.json "audio": {"peak_dbfs": -6, "dialogue_lufs": -19.5, "channels": [0, 1]}  (these are the defaults)
"""
import json
import os
import re
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import expand, fps_value, load_edl, probe  # noqa: E402

SR = 48000
LEVEL = "dynaudnorm=f=200:g=11:p=0.5:m=10:s=0"            # ~2 s smoothing: follows speakers, not syllables


def limiter(peak_dbfs):
    """Brick-wall limit at the ceiling, run 4x oversampled so inter-sample (true) peaks stay under it too;
    dense music otherwise overshoots by 1-2 dB once it's encoded to AAC."""
    return (f"aresample={SR * 4},alimiter=limit={10 ** (peak_dbfs / 20):.4f}:attack=3:release=60:level=false:latency=true,"
            f"aresample={SR}")


def pcm(src, start, dur, af):
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.6f}", "-t", f"{dur:.6f}", "-i", src, "-af", af,
                          "-ar", str(SR), "-ac", "2", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.float32).reshape(-1, 2)


def loudness(x):
    r = subprocess.run(["ffmpeg", "-nostats", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-", "-af", "ebur128",
                        "-f", "null", "-"], input=np.ascontiguousarray(x).tobytes(), capture_output=True).stderr.decode()
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", r)[-1])


MAX_LIFT_DB = 6.0
SILENT_LUFS = -60.0


def true_peak_db(x):
    """Peak of the 4x-oversampled signal, so inter-sample peaks count (what an AAC encode will show)."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
                          "-af", f"aresample={SR * 4}", "-f", "f32le", "-"],
                         input=np.ascontiguousarray(x).tobytes(), capture_output=True, check=True).stdout
    return float(20 * np.log10(np.abs(np.frombuffer(out, np.float32)).max() + 1e-9))


def to_peak(x, peak_dbfs):
    """One static gain so the true peak sits at peak_dbfs. Cuts are unlimited; boosts stop at MAX_LIFT_DB.
    Returns (signal, gain in dB, whether the ceiling was reached)."""
    gain = peak_dbfs - true_peak_db(x)
    reached = gain <= MAX_LIFT_DB
    gain = min(gain, MAX_LIFT_DB)
    return x * np.float32(10 ** (gain / 20)), gain, reached


def fit(x, n):
    """Exactly n samples: pad with silence or trim, so audio and picture line up to the sample."""
    return x[:n] if len(x) >= n else np.vstack([x, np.zeros((n - len(x), 2), np.float32)])


def write(x, path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
                    "-c:a", "pcm_s24le", path], input=np.ascontiguousarray(x).tobytes(), check=True)


def main():
    show_dir = sys.argv[1]
    edl = load_edl(show_dir)
    fps = fps_value(edl["fps"])
    cfg = {"peak_dbfs": -6.0, "dialogue_lufs": -19.5, "channels": [0, 1], **edl.get("audio", {})}
    ch = cfg["channels"]
    pan = f"pan=stereo|c0=c{ch[0]}|c1=c{ch[-1]}"
    out_dir = os.path.join(show_dir, "Audio")
    os.makedirs(out_dir, exist_ok=True)
    report = []
    for seg in edl["segments"]:
        if seg["kind"] == "placeholder":
            continue
        out = os.path.join(out_dir, f"{seg['id']}.wav")
        if seg["kind"] == "source":
            frames = seg["out"] - seg["in"]
            start, dur = seg["in"] / fps, frames / fps
            first = pcm(expand(edl["source"], edl), start, dur, f"{pan},{LEVEL},{limiter(cfg['peak_dbfs'])}")
            measured = loudness(first)
            trim = cfg["dialogue_lufs"] - measured if measured > SILENT_LUFS else 0.0
            x = pcm(expand(edl["source"], edl), start, dur,
                    f"{pan},{LEVEL},volume={trim:.2f}dB,{limiter(cfg['peak_dbfs'])}")
        else:
            path = expand(seg["path"], edl)
            frames = round(float(probe(path)["format"]["duration"]) * fps)   # what the timeline will show
            dur = frames / fps
            raw = pcm(path, 0, dur, "anull")
            if seg.get("audio") == "match":       # packages: already mixed, so match the anchors' loudness + limit
                measured = loudness(raw)
                trim = cfg["dialogue_lufs"] - measured if measured > SILENT_LUFS else 0.0
                x = pcm(path, 0, dur, f"volume={trim:.2f}dB,{limiter(cfg['peak_dbfs'])}")
            else:                                 # intro / outro music: the peak gain below does it all
                trim, x = 0.0, raw
        x = fit(x, int(np.ceil(frames / fps * SR)) + int(SR / fps))   # one spare frame: the builder trims to the exact frame
        x, lift, reached = to_peak(x, cfg["peak_dbfs"])
        write(x, out)
        peak = true_peak_db(x)
        report.append({"id": seg["id"], "file": out, "trim_db": round(float(trim), 2), "lift_db": round(float(lift), 2),
                       "lufs": round(loudness(x), 1), "peak_dbfs": round(peak, 1), "reached": bool(reached),
                       "seconds": round(len(x) / SR, 3)})
        note = "" if reached else f"   ⚠️ can't reach {cfg['peak_dbfs']} dBFS within +{MAX_LIFT_DB:.0f} dB (near silence?)"
        print(f"{seg['id']:22} trim {trim:+6.2f} dB  lift {lift:+5.2f} dB   {report[-1]['lufs']:6.1f} LUFS   "
              f"true peak {peak:5.1f} dBFS{note}", flush=True)
    with open(os.path.join(out_dir, "levels.json"), "w") as f:
        json.dump({"settings": cfg, "segments": report}, f, indent=1)


if __name__ == "__main__":
    main()
