"""ffprobe/ffmpeg helpers: probe a file, join a show split into several files."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

Run = Callable[..., subprocess.CompletedProcess]


class MediaError(Exception):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    video_codec: str | None
    audio_codec: str | None

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None


def parse_probe(raw: str) -> MediaInfo:
    data = json.loads(raw or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float((data.get("format") or {}).get("duration") or 0)
    return MediaInfo(
        duration=duration,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        video_codec=(video or {}).get("codec_name"),
        audio_codec=(audio or {}).get("codec_name"),
    )


def probe(path: Path, run: Run = subprocess.run) -> MediaInfo:
    proc = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
               capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise MediaError(f"ffprobe couldn't read {path.name}: {(proc.stderr or '').strip()[:200]}")
    return parse_probe(proc.stdout)


def join_sources(sources: Sequence[Path], out_dir: Path, run: Run = subprocess.run) -> Path:
    """One file: use it as is. Several: concat losslessly when they match,
    otherwise re-encode to ProRes 422 HQ. Returns the joined source path."""
    if not sources:
        raise MediaError("No source files")
    if len(sources) == 1:
        target = out_dir / f"source{sources[0].suffix.lower()}"
        if not target.exists():
            os.link(sources[0], target)  # keeps src/ intact for a retried attempt
        return target
    infos = [probe(p, run) for p in sources]
    same = len({(i.video_codec, i.width, i.height, i.audio_codec) for i in infos}) == 1
    list_file = out_dir / "sources.txt"
    list_file.write_text("".join(f"file '{_escape(p)}'\n" for p in sources))
    target = out_dir / "source.mov"
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(list_file)]
    if same:
        cmd += ["-c", "copy"]
    else:
        cmd += ["-c:v", "prores_ks", "-profile:v", "3", "-c:a", "pcm_s24le"]
    proc = run(cmd + [str(target)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise MediaError(f"Couldn't join the recording files: {(proc.stderr or '').strip()[:300]}")
    return target


def _escape(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")
