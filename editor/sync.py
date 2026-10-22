"""Aligning separately recorded camera feeds by their audio.

Each feed's audio is read as 8 kHz mono, reduced to a loudness envelope, and
cross-correlated (numpy FFT) with the reference (M, or the separate audio file).
The offset d of feed X means: X at time t + d shows the same moment as the
reference at time t. Confidence is the correlation peak over the next-highest
peak at least half a second away; below MIN_CONFIDENCE the audio match is weak.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

SAMPLE_RATE = 8000
ENV_RATE = 1000  # envelopes are kept at 1 kHz: 1 ms steps, far finer than a 59.94 frame
ENVELOPE_MS = 10
MAX_SYNC_S = 600  # the first 10 minutes are plenty to find the offset
MIN_CONFIDENCE = 2.0
EXCLUDE_S = 0.5

Run = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class SyncResult:
    offset_s: float
    confidence: float


def read_audio(path: Path, run: Run = subprocess.run, seconds: float = MAX_SYNC_S) -> np.ndarray | None:
    """The first audio stream as 8 kHz mono float32, or None if the file has no audio."""
    proc = run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-t", str(seconds), "-vn",
                "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"], capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, dtype=np.float32).astype(np.float64)


def envelope(samples: np.ndarray, rate: int = SAMPLE_RATE) -> np.ndarray:
    """Loudness envelope at ENV_RATE: |x| smoothed over 10 ms, decimated, zero-mean, unit norm."""
    width = max(1, rate * ENVELOPE_MS // 1000)
    env = np.convolve(np.abs(samples), np.ones(width) / width, mode="same")
    step = max(1, rate // ENV_RATE)
    env = env[::step]
    env = env - env.mean()
    norm = np.linalg.norm(env)
    return env / norm if norm > 0 else env


def correlate(reference: np.ndarray, other: np.ndarray, rate: int = SAMPLE_RATE) -> SyncResult:
    """Offset of `other` against `reference`, from raw samples at `rate`."""
    return correlate_envelopes(envelope(reference, rate), envelope(other, rate))


def correlate_envelopes(a: np.ndarray, b: np.ndarray, rate: int = ENV_RATE) -> SyncResult:
    """Offset d with b(t + d) ≈ a(t), and the peak ratio, from two envelopes at `rate`."""
    if not len(a) or not len(b):
        return SyncResult(0.0, 0.0)
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    peak = int(np.argmax(corr))
    best = float(corr[peak])
    exclude = int(EXCLUDE_S * rate)
    lags = np.arange(n)
    distance = np.minimum(np.abs(lags - peak), n - np.abs(lags - peak))
    rest = corr[distance > exclude]
    second = float(rest.max()) if rest.size else 0.0
    confidence = best / second if second > 0 else float("inf")
    lag = peak if peak <= n // 2 else peak - n  # reference[t + lag] ≈ other[t]
    return SyncResult(offset_s=-lag / rate, confidence=confidence)


def audio_envelope(path: Path, run: Run = subprocess.run) -> np.ndarray | None:
    samples = read_audio(path, run)
    return envelope(samples) if samples is not None and samples.size else None
