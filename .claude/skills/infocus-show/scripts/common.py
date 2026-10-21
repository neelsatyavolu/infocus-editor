"""Shared paths, constants and EDL helpers for the InFocus show scripts (stdlib only)."""
import json
import os
import subprocess

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
WHISPER_MODEL = os.path.expanduser("~/.cache/whisper-cpp/ggml-large-v3-turbo.bin")
MATTER = os.path.join(SCRIPTS, "bin", "personmatte_video")

W, H = 1920, 1080
# TV screens on the 2026 Left/Right virtual sets, in 1080p frame pixels (2 px bleed included). The switcher's
# side cameras show the whole set, so these are also the screen positions in the program recording.
SCREENS = {"L": (55, 112, 1124, 630), "R": (741, 108, 1122, 633)}
# Where the package overlays sit (1080p): name bars on the title-safe band, Follow card top left.
NAMEBAR_BAND = (150, 840, 1770, 975)
FOLLOW_BOX = (48, 40, 700, 300)


def fps_value(fps):
    """'60000/1001' | '29.97' | 59.94 -> float."""
    if isinstance(fps, (int, float)):
        return float(fps)
    if "/" in fps:
        num, den = (float(v) for v in fps.split("/"))
        return num / den
    return float(fps)


def package_dir(edl=None):
    """The InFocus 2026 Package folder: edl.json "package" (copied from brief.packageDir), else $INFOCUS_PACKAGE."""
    path = (edl or {}).get("package") or os.environ.get("INFOCUS_PACKAGE")
    if not path:
        raise RuntimeError('No package folder: set "package" in edl.json (brief.packageDir) or INFOCUS_PACKAGE')
    return os.path.expanduser(path)


def seek_args(frame, fps):
    """Frame-exact positioning: seek to just before frame j (about a second ahead of the wanted frame), then pick
    by index from there. Returns (ffmpeg input args, index of the wanted frame among the decoded frames)."""
    j = max(0, frame - int(fps))
    if j == 0:
        return [], frame
    return ["-ss", f"{(j - 0.5) / fps:.6f}"], frame - j


def expand(path, edl=None):
    """Paths in edl.json may use {PACKAGE} and ~."""
    if "{PACKAGE}" in path:
        path = path.replace("{PACKAGE}", package_dir(edl))
    return os.path.expanduser(path)


def load_edl(show_dir):
    with open(os.path.join(show_dir, "edl.json")) as f:
        return json.load(f)


def card_segments(edl):
    """Source segments that carry an announcement card on the set TV."""
    return [s for s in edl["segments"] if s.get("kind") == "source" and s.get("card")]


# File names, shared by the render, matte and build steps.
def card_file(show_dir, seg):
    return os.path.join(show_dir, "Graphics", f"{seg['id']} - Monitor.mov")


def foreground_file(show_dir, seg):
    return os.path.join(show_dir, "Foreground", f"{seg['id']} - Foreground.mov")


def slate_file(show_dir, seg):
    return os.path.join(show_dir, "Graphics", f"{seg['id']} - Slate.png")


def overlay_file(show_dir, ov, edl=None):
    return expand(ov["file"], edl) if ov.get("file") else os.path.join(show_dir, "Graphics", f"{ov['id']}.mov")


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type,width,height,r_frame_rate,nb_frames,channels",
                          "-of", "json", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def ensure_matter():
    """Compile the Apple Vision person-matte tool on first use."""
    if not os.path.exists(MATTER):
        os.makedirs(os.path.dirname(MATTER), exist_ok=True)
        subprocess.run(["swiftc", "-O", os.path.join(SCRIPTS, "personmatte_video.swift"), "-o", MATTER], check=True)
    return MATTER
