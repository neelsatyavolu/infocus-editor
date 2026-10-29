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


# Seasonal looks. A theme has its own intro, outro, Follow card, TV standby and sets, installed inside the package
# under Themes/<Name>/; its name bars and cards come from the package's graphics.html (?theme=<name>). "" is the
# regular 2026 look.
THEMES = {
    "": {
        "intro": "03 Intro & Outro/InFocus Intro 2026.mp4",
        "outro": "03 Intro & Outro/InFocus Outro 2026.mp4",
        "follow": "08 Follow InFocus/InFocus Follow Us.mov",
        "standby": "09 TV Standby/InFocus TV Standby.png",
        "left": "04 Virtual Sets/InFocus Virtual Set 2026 - Left.png",
        "middle": "04 Virtual Sets/InFocus Virtual Set 2026 - Middle.png",
        "right": "04 Virtual Sets/InFocus Virtual Set 2026 - Right.png",
    },
    "halloween": {
        "intro": "Themes/Halloween/03 Intro & Outro/InFocus Intro Halloween 2026.mp4",
        "outro": "Themes/Halloween/03 Intro & Outro/InFocus Outro Halloween 2026.mp4",
        "follow": "Themes/Halloween/08 Follow InFocus/InFocus Follow Us (Halloween).mov",
        "standby": "Themes/Halloween/09 TV Standby/InFocus TV Standby (Halloween).png",
        "left": "Themes/Halloween/04 Virtual Sets/InFocus Virtual Set Halloween 2026 - Left.png",
        "middle": "Themes/Halloween/04 Virtual Sets/InFocus Virtual Set Halloween 2026 - Middle.png",
        "right": "Themes/Halloween/04 Virtual Sets/InFocus Virtual Set Halloween 2026 - Right.png",
    },
    "thanksgiving": {
        "intro": "Themes/Thanksgiving/03 Intro & Outro/InFocus Intro Thanksgiving 2026.mp4",
        "outro": "Themes/Thanksgiving/03 Intro & Outro/InFocus Outro Thanksgiving 2026.mp4",
        "follow": "Themes/Thanksgiving/08 Follow InFocus/InFocus Follow Us (Thanksgiving).mov",
        "standby": "Themes/Thanksgiving/09 TV Standby/InFocus TV Standby (Thanksgiving).png",
        "left": "Themes/Thanksgiving/04 Virtual Sets/InFocus Virtual Set Thanksgiving 2026 - Left.png",
        "middle": "Themes/Thanksgiving/04 Virtual Sets/InFocus Virtual Set Thanksgiving 2026 - Middle.png",
        "right": "Themes/Thanksgiving/04 Virtual Sets/InFocus Virtual Set Thanksgiving 2026 - Right.png",
    },
}


def theme_name(edl=None):
    """The show's theme: edl.json "theme" when present (an empty one picks the regular look), else $INFOCUS_THEME,
    else "" (the regular look). render_graphics.mjs reads it the same way."""
    edl = edl or {}
    name = str(edl["theme"] if edl.get("theme") is not None else os.environ.get("INFOCUS_THEME") or "").strip().lower()
    if name not in THEMES:
        raise RuntimeError(f'Unknown theme "{name}" (known: {", ".join(t for t in THEMES if t)})')
    return name


def theme_file(key, edl=None, package=None):
    """Absolute path of one of the theme's package files ("intro", "outro", "follow", "standby", "left", "middle",
    "right")."""
    return os.path.join(package or package_dir(edl), THEMES[theme_name(edl)][key])


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


def recordings(edl):
    """The show's recordings by id, from edl.json "recordings" (copied from the worker's show/recordings.json):
    [{"id", "kind": "program"|"multicam", "frames", "files": [{"path", "role": "program"|"M"|"L"|"R",
    "offsetFrames"}]}]. Frame t of a recording is frame t + offsetFrames of each of its files. An edl with only
    "source" (one switched recording) is recording "r1"."""
    if edl.get("recordings"):
        return {r["id"]: r for r in edl["recordings"]}
    return {"r1": {"id": "r1", "kind": "program", "files": [{"path": edl["source"], "role": "program",
                                                             "offsetFrames": 0}]}}


def recording_of(edl, item):
    """The recording a segment or overlay belongs to ("recording": "r1"; optional when there's only one)."""
    recs = recordings(edl)
    rid = item.get("recording") or (next(iter(recs)) if len(recs) == 1 else None)
    if rid not in recs:
        raise RuntimeError(f"{item.get('id')}: name its recording ({', '.join(recs)}) with \"recording\"")
    return recs[rid]


def is_multicam(edl, item):
    return recording_of(edl, item)["kind"] == "multicam"


def _file(rec, role):
    return next((f for f in rec["files"] if f["role"] == role), None)


def segment_source(edl, seg):
    """(file, frame offset) a source segment's picture comes from: frame f of the segment is frame f + offset of the
    file. Program recording: its program file. Multicam recording: the file of the segment's camera ("cam")."""
    rec = recording_of(edl, seg)
    f = _file(rec, "program") if rec["kind"] == "program" else _file(rec, seg.get("cam"))
    if not f:
        raise RuntimeError(f"segment {seg['id']}: recording {rec['id']} has no {seg.get('cam')!r} camera file")
    return expand(f["path"], edl), int(f.get("offsetFrames", 0))


def segment_audio(edl, seg):
    """(file, frame offset) a source segment's sound comes from: the recording's M file, else its program file."""
    rec = recording_of(edl, seg)
    f = _file(rec, "M") or _file(rec, "program") or rec["files"][0]
    return expand(f["path"], edl), int(f.get("offsetFrames", 0))


def card_segments(edl):
    """Source segments that carry an announcement card on the set TV."""
    return [s for s in edl["segments"] if s.get("kind") == "source" and s.get("card")]


# File names, shared by the render, matte and build steps.
def card_file(show_dir, seg):
    return os.path.join(show_dir, "Graphics", f"{seg['id']} - Monitor.mov")


def foreground_file(show_dir, seg):
    return os.path.join(show_dir, "Foreground", f"{seg['id']} - Foreground.mov")


def recomposite_file(show_dir, seg):
    return os.path.join(show_dir, "Recomposite", f"{seg['id']} - Recomposite.mov")


def recomposite_on(edl):
    """Whether the show's takes are re-composited onto the clean virtual set (edl.json "recomposite")."""
    return bool(edl.get("recomposite"))


def is_fixup(overlay, show_dir):
    """A fix-up clip made from the recording's own frames (fixups.py writes them to Graphics/fixes)."""
    path = overlay.get("file") or ""
    return os.path.join("Graphics", "fixes") in path or path.startswith(os.path.join(show_dir, "Graphics", "fixes"))


def recomposited_takes(show_dir, edl):
    """Ids of the takes whose picture is their re-composited clip (Recomposite/plan.json status "recomposited"),
    unless recomposite is off or the pass was abandoned."""
    path = os.path.join(show_dir, "Recomposite", "plan.json")
    if not recomposite_on(edl) or not os.path.exists(path):
        return set()
    with open(path) as f:
        plan = json.load(f)
    if plan.get("abandoned"):
        return set()
    return {t["id"] for t in plan.get("takes", []) if t.get("status") == "recomposited"}


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


def ensure_tool(name):
    """A Swift helper next to the scripts (anchor_matte, face_boxes, face_marks), compiled into bin/ on first use."""
    exe = os.path.join(SCRIPTS, "bin", name)
    if not os.path.exists(exe):
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        subprocess.run(["swiftc", "-O", os.path.join(SCRIPTS, f"{name}.swift"), "-o", exe], check=True)
    return exe
