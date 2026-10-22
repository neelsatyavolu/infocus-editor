"""ffprobe/ffmpeg helpers: probe a file, conform a feed to the house format."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

Run = Callable[..., subprocess.CompletedProcess]

HOUSE_FPS = Fraction(60000, 1001)
HOUSE_SIZE = (1920, 1080)
PRORES_HQ = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s24le"]


class MediaError(Exception):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    video_codec: str | None
    audio_codec: str | None
    fps: Fraction | None = None

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def is_house_format(self) -> bool:
        return (self.width, self.height) == HOUSE_SIZE and self.fps == HOUSE_FPS

    def frames(self, fps: Fraction | None = None) -> int:
        return round(self.duration * float(fps or self.fps or HOUSE_FPS))


def _fps(text: str | None) -> Fraction | None:
    try:
        value = Fraction(text) if text else None
    except (ValueError, ZeroDivisionError):
        return None
    return value if value else None


def parse_probe(raw: str) -> MediaInfo:
    data = json.loads(raw or "{}")
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None) or {}
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None) or {}
    return MediaInfo(
        duration=float(fmt.get("duration") or 0),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name"),
        fps=_fps(video.get("r_frame_rate")),
    )


def probe(path: Path, run: Run = subprocess.run) -> MediaInfo:
    proc = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
               capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise MediaError(f"ffprobe couldn't read {path.name}: {(proc.stderr or '').strip()[:200]}")
    return parse_probe(proc.stdout)


def conform_args() -> list[str]:
    """To 1080p59.94 ProRes 422 HQ (scaled with letterboxing if the aspect differs)."""
    w, h = HOUSE_SIZE
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
          f"fps={HOUSE_FPS.numerator}/{HOUSE_FPS.denominator}")
    return ["-vf", vf, *PRORES_HQ]


def conform(src: Path, dest: Path, run: Run = subprocess.run) -> Path:
    proc = run(["ffmpeg", "-y", "-v", "error", "-i", str(src), *conform_args(), str(dest)],
               capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise MediaError(f"Couldn't convert {src.name} to 1080p59.94: {(proc.stderr or '').strip()[:300]}")
    return dest
