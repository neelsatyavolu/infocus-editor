"""Builds and renders a show from <show_dir>/edl.json with one ffmpeg run (no editing app needed).

    uv run --with numpy python build_ffmpeg.py <show_dir> --output <file.mp4> [--version N]

The picture is stacked the way an editor timeline would be:
  V1  program: intro, chosen takes (trimmed by frame index), packages, placeholder slates, outro. Each take reads
      its recording (edl "recordings"): a program recording's program file, or in a multicam recording the file of
      the take's camera ("cam"), at in + that file's offset, so a multicam show's takes are its switching plan.
      Files at another
      rate are conformed to the show rate (60000/1001); a 29.97 clip shows each frame twice
  V2  fresh TV cards, scaled onto the set TV screen (SCREENS in common.py), for every source segment with a "card"
  V3  anchor foregrounds from foreground.py, so shoulders and hair stay in front of the cards
      (a take recomposite.py re-composited reads its Recomposite/ clip instead, card and anchors already in it: no
      V2 or V3)
  V4  overlays (name bars, dual thirds, Follow card, fix-up clips), from their "at" frame (on their "recording")
      for the clip's whole length
  A1  the mastered WAVs from master_audio.py, each cut to its segment's exact picture length
Frame rules: a segment's "out" is exclusive; a clip at another rate lasts floor(frames x show rate / clip rate)
show frames (a 828-frame 60 fps intro is 827 frames at 59.94; a 153-frame 29.97 outro is 306).

Writes <file.mp4> (H.264 VideoToolbox at a high bitrate, libx264 if VideoToolbox isn't available, AAC 320k), plus
<show_dir>/layout.json (where every segment, card and overlay landed, in show frames from 0; check_render.py and
verify_render.py read it) and <show_dir>/timeline.fcpxml (the same edit for a hand fix in an editing app).
Give every version its own output name (e.g. "... v3.mp4").
"""
import json
import math
import os
import subprocess
import sys
import tempfile
from fractions import Fraction

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (H, SCREENS, W, card_file, expand, foreground_file, fps_value, load_edl, overlay_file,  # noqa: E402
                    probe, recomposite_file, recomposited_takes, recording_of, seek_args, segment_source,
                    slate_file)

SR = 48000
VIDEO_BITRATE = "30M"          # about what the old H.264 Master preset produced for a 1080p59.94 show
OVERLAY_FPS = "30000/1001"


def clip_info(path):
    info = probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    rate = Fraction(v["r_frame_rate"])
    frames = int(v.get("nb_frames") or 0) or round(float(info["format"]["duration"]) * rate)
    return {"rate": rate, "frames": frames, "width": int(v["width"]), "height": int(v["height"]),
            "audio": any(s["codec_type"] == "audio" for s in info["streams"])}


def conformed(frames, clip_rate, show_rate):
    """How many show frames a clip lasts once conformed (by duration, rounded down)."""
    return int(math.floor(frames * Fraction(show_rate) / Fraction(clip_rate) + Fraction(1, 10**6)))


def plan(edl, show_dir):
    """Where everything lands, in show frames: the same layout an editor timeline would produce."""
    rate = Fraction(edl["fps"])
    rec = 0
    segments, cards, spans = [], [], []
    redone = recomposited_takes(show_dir, edl)
    for seg in edl["segments"]:
        if seg["kind"] == "file":
            path = expand(seg["path"], edl)
            info = clip_info(path)
            n = conformed(info["frames"], info["rate"], rate)
            item = {"seg": seg, "path": path, "info": info}
        elif seg["kind"] == "placeholder":
            n = round(seg["seconds"] * rate)
            item = {"seg": seg, "path": slate_file(show_dir, seg)}
        else:
            n = seg["out"] - seg["in"]
            path, offset = segment_source(edl, seg)
            if seg["id"] in redone:                                  # the clip starts at the take's "in"
                path, offset = recomposite_file(show_dir, seg), -seg["in"]
                if not os.path.exists(path):
                    raise RuntimeError(f"take {seg['id']}: no re-composited clip at {path}; run recomposite.py")
            item = {"seg": seg, "path": path, "offset": offset, "recomposited": seg["id"] in redone}
            spans.append((recording_of(edl, seg)["id"], seg["in"], seg["out"], rec))
            if seg.get("card"):
                cards.append({"id": seg["id"], "rec_in": rec, "rec_out": rec + n})
        item.update(rec_in=rec, rec_out=rec + n)
        segments.append(item)
        rec += n
    overlays = []
    for o in edl.get("overlays", []):
        rid = recording_of(edl, o)["id"]
        hits = [r + (o["at"] - a) for sid, a, b, r in spans if sid == rid and a <= o["at"] < b]
        if not hits:
            raise RuntimeError(f"overlay {o['id']}: frame {o['at']} of recording {rid} is outside every chosen take")
        path = overlay_file(show_dir, o, edl)
        info = clip_info(path)
        n = conformed(info["frames"], info["rate"], rate)
        overlays.append({"o": o, "path": path, "info": info, "rec_in": hits[0], "rec_out": hits[0] + n})
    return segments, cards, overlays, rec


def layout_json(edl, version, segments, cards, overlays, total):
    return {"timeline": f"{edl['show']} v{version}", "fps": edl["fps"],
            "segments": [{"id": s["seg"]["id"], "kind": s["seg"]["kind"], "rec_in": s["rec_in"], "rec_out": s["rec_out"],
                          **({"recomposite": True} if s.get("recomposited") else {})} for s in segments],
            "cards": cards,
            "overlays": [{"id": x["o"]["id"], "rec_in": x["rec_in"], "rec_out": x["rec_out"],
                          "covers": x["o"].get("covers")} for x in overlays],
            "frames": total}


def conform_chain(label, clip_rate, show_rate, n, alpha):
    """Retime a clip to the show rate (frame k of the clip lands on show frame ceil(k x show/clip)), fit it to
    1920x1080 and keep exactly n frames."""
    fmt = "yuva444p" if alpha else "yuv444p"
    return (f"[{label}]setpts=N/({clip_rate})/TB,fps={show_rate}:round=up,"
            f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black@0,setsar=1,trim=end_frame={n},setpts=PTS-STARTPTS,format={fmt}")


def filtergraph(edl, show_dir, segments, overlays, total):
    """ffmpeg inputs and the filtergraph that stacks V1-V4."""
    rate = edl["fps"]
    show_rate = Fraction(rate)
    fps = float(show_rate)
    tick = 1 / show_rate
    tb = f"{tick.numerator}/{tick.denominator}"                # one time-base tick per show frame
    inputs, chains, parts = [], [], []

    def add_input(args):
        inputs.append(args)
        return f"{len(inputs) - 1}:v"

    for i, s in enumerate(segments):
        seg, n = s["seg"], s["rec_out"] - s["rec_in"]
        out = f"s{i}"
        if seg["kind"] == "source":
            pre, k = seek_args(seg["in"] + s["offset"], fps)
            src = add_input([*pre, "-i", s["path"]])
            chains.append(f"[{src}]trim=start_frame={k}:end_frame={k + n},setpts=PTS-STARTPTS,fps={rate},"
                          f"setsar=1,format=yuv444p[{out}b]")
            last = f"{out}b"
            if seg.get("card") and not s.get("recomposited"):        # a re-composited take has its card in it
                x, y, w, h = SCREENS[seg["cam"]]
                card = add_input(["-i", card_file(show_dir, seg)])
                fg = add_input(["-i", foreground_file(show_dir, seg)])
                chains.append(f"[{card}]setpts=PTS-STARTPTS,scale={w}:{h}:flags=lanczos,format=yuva444p[{out}c]")
                chains.append(f"[{fg}]setpts=PTS-STARTPTS,format=yuva444p[{out}f]")
                chains.append(f"[{last}][{out}c]overlay={x}:{y}:eof_action=pass:format=yuv444[{out}bc]")
                chains.append(f"[{out}bc][{out}f]overlay=0:0:eof_action=pass:format=yuv444[{out}bcf]")
                last = f"{out}bcf"
            chains.append(f"[{last}]trim=end_frame={n},setpts=PTS-STARTPTS,settb={tb}[{out}]")
        elif seg["kind"] == "file":
            src = add_input(["-i", s["path"]])
            chains.append(conform_chain(src, s["info"]["rate"], show_rate, n, False) + f",settb={tb}[{out}]")
        else:
            src = add_input(["-loop", "1", "-framerate", rate, "-i", s["path"]])
            chains.append(f"[{src}]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
                          f"setsar=1,trim=end_frame={n},setpts=PTS-STARTPTS,format=yuv444p,settb={tb}[{out}]")
        parts.append(f"[{out}]")
    # Re-stamp by frame index: every segment is constant-rate and gap-free, so the n-th frame IS show frame n.
    # (Timestamps carried through concat drifted by a frame after conformed or composited segments.)
    chains.append(f"{''.join(parts)}concat=n={len(parts)}:v=1:a=0,settb={tb},setpts=N[v1]")
    last = "v1"
    for j, x in enumerate(overlays):
        src = add_input(["-i", x["path"]])
        n = x["rec_out"] - x["rec_in"]
        # Lead the overlay in with rec_in transparent frames and stamp it by index too, so it lines up with the
        # program frame for frame.
        chains.append(conform_chain(src, x["info"]["rate"], show_rate, n, True) +
                      f",tpad=start={x['rec_in']}:color=black@0,settb={tb},setpts=N[o{j}]")
        chains.append(f"[{last}][o{j}]overlay=0:0:eof_action=pass:format=yuv444:"
                      f"enable='between(n\\,{x['rec_in']}\\,{x['rec_out'] - 1})'[v{j + 2}]")
        last = f"v{j + 2}"
    chains.append(f"[{last}]trim=end_frame={total},format=yuv420p[vout]")
    return inputs, ";".join(chains)


def mastered_mix(edl, show_dir, segments, total, out_wav):
    """A1: each segment's mastered WAV cut to its exact picture length (sample-exact against the show frames);
    silence under placeholders."""
    rate = Fraction(edl["fps"])

    def sample(frame):
        return int(round(frame / rate * SR))
    mix = np.zeros((sample(total), 2), np.float32)
    for s in segments:
        a, b = sample(s["rec_in"]), sample(s["rec_out"])
        if s["seg"]["kind"] == "placeholder":
            continue
        wav = os.path.join(show_dir, "Audio", f"{s['seg']['id']}.wav")
        if not os.path.exists(wav):
            raise RuntimeError(f"missing {wav}: run master_audio.py first")
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", wav, "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                             capture_output=True, check=True).stdout
        x = np.frombuffer(raw, np.float32).reshape(-1, 2)[:b - a]
        mix[a:a + len(x)] = x
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", "-",
                    "-c:a", "pcm_s24le", out_wav], input=mix.tobytes(), check=True)


def encoders():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "h264_videotoolbox" in out


def video_frames(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out and out[0].isdigit() else -1         # ffprobe may repeat it for a stream group


def encode(inputs, graph, wav, output, rate, total):
    """Render with VideoToolbox, then libx264 if VideoToolbox is missing, errors, or drops frames (it does that
    silently when the hardware encoder is busy). The result always has exactly `total` frames."""
    video_codecs = []
    if encoders():
        # no -maxrate: VideoToolbox drops frames to honour a data-rate limit
        video_codecs.append(["-c:v", "h264_videotoolbox", "-b:v", VIDEO_BITRATE, "-profile:v", "high",
                             "-allow_sw", "0"])
    video_codecs.append(["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-profile:v", "high"])
    tmp = output + ".part.mp4"
    last_error = ""
    for codec in video_codecs:
        cmd = ["ffmpeg", "-v", "error", "-y"]
        for args in inputs:
            cmd += args
        cmd += ["-i", wav, "-filter_complex", graph, "-map", "[vout]", "-map", f"{len(inputs)}:a",
                *codec, "-r", rate, "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:a", "aac", "-b:a", "320k", "-ar", str(SR), "-movflags", "+faststart", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True)
        got = video_frames(tmp) if r.returncode == 0 else -1
        if r.returncode == 0 and got == total and "Error encoding frame" not in r.stderr:
            os.replace(tmp, output)
            return codec[1]
        last_error = r.stderr[-2000:] or f"{codec[1]} wrote {got} frames, expected {total}"
        print(f"build_ffmpeg: {codec[1]} failed ({last_error.strip().splitlines()[-1]}); trying the next encoder",
              file=sys.stderr, flush=True)
        if os.path.exists(tmp):
            os.remove(tmp)
    raise RuntimeError(f"ffmpeg render failed: {last_error}")


# ---------- FCPXML ----------

def _t(frames, rate):
    """A show-frame count as an FCPXML rational time."""
    d = Fraction(frames) / Fraction(rate)
    return f"{d.numerator}/{d.denominator}s" if d.denominator != 1 else f"{d.numerator}s"


def fcpxml(edl, show_dir, segments, cards, overlays, total, version):
    from urllib.parse import quote
    from xml.sax.saxutils import quoteattr
    rate = edl["fps"]
    fd = 1 / Fraction(rate)
    assets, lines = {}, []

    def asset(path, video=True, audio=False):
        if path not in assets:
            aid = f"r{len(assets) + 2}"
            seconds = Fraction(0)
            if not path.lower().endswith(".png"):
                seconds = Fraction(probe(path)["format"]["duration"]).limit_denominator(60000)
            assets[path] = (aid, video, audio, seconds)
        return assets[path][0]

    def url(path):
        return "file://" + quote(os.path.abspath(path))

    by_rec = []
    spine = []
    for s in segments:
        seg, n = s["seg"], s["rec_out"] - s["rec_in"]
        start = Fraction(seg["in"] + s["offset"]) / Fraction(rate) if seg["kind"] == "source" else Fraction(0)
        start_s = f"{start.numerator}/{start.denominator}s"
        kind = "video" if seg["kind"] == "placeholder" else "asset-clip"
        aid = asset(s["path"], True, seg["kind"] != "placeholder" and not s.get("recomposited"))
        children = []
        if seg["kind"] != "placeholder":
            wav = os.path.join(show_dir, "Audio", f"{seg['id']}.wav")
            if os.path.exists(wav):
                children.append(f'<asset-clip ref="{asset(wav, False, True)}" lane="-1" offset="{start_s}" '
                                f'start="0s" duration="{_t(n, rate)}" name="{seg["id"]} (mastered)"/>')
        if seg.get("card") and not s.get("recomposited"):
            x, y, w, h = SCREENS[seg["cam"]]
            px = ((x + w / 2) - W / 2) / H * 100
            py = (H / 2 - (y + h / 2)) / H * 100
            children.append(f'<asset-clip ref="{asset(card_file(show_dir, seg))}" lane="1" offset="{start_s}" '
                            f'start="0s" duration="{_t(n, rate)}" name="{seg["id"]} card">'
                            f'<adjust-transform position="{px:.4f} {py:.4f}" scale="{w / W:.6f} {h / H:.6f}"/>'
                            f'</asset-clip>')
            children.append(f'<asset-clip ref="{asset(foreground_file(show_dir, seg))}" lane="2" offset="{start_s}" '
                            f'start="0s" duration="{_t(n, rate)}" name="{seg["id"]} foreground"/>')
        spine.append([kind, aid, start, n, seg, children, s])
        by_rec.append((s["rec_in"], s["rec_out"], len(spine) - 1))
    for x in overlays:
        idx = next(i for a, b, i in by_rec if a <= x["rec_in"] < b)
        parent = spine[idx]
        offset = parent[2] + Fraction(x["rec_in"] - parent[6]["rec_in"]) * fd
        parent[5].append(f'<asset-clip ref="{asset(x["path"])}" lane="3" offset="{offset.numerator}/{offset.denominator}s" '
                         f'start="0s" duration="{_t(x["rec_out"] - x["rec_in"], rate)}" name={quoteattr(x["o"]["id"])}/>')
    for kind, aid, start, n, seg, children, s in spine:
        start_s = f"{start.numerator}/{start.denominator}s"
        attrs = (f'ref="{aid}" offset="{_t(s["rec_in"], rate)}" start="{start_s}" duration="{_t(n, rate)}" '
                 f'name={quoteattr(seg["id"])}')
        lines.append(f"<{kind} {attrs}>{''.join(children)}</{kind}>")
    res = [f'<format id="r1" name="FFVideoFormat1080p5994" frameDuration="{fd.numerator}/{fd.denominator}s" '
           f'width="{W}" height="{H}"/>']
    for path, (aid, video, audio, seconds) in assets.items():
        dur = f"{seconds.numerator}/{seconds.denominator}s"
        res.append(f'<asset id="{aid}" name={quoteattr(os.path.basename(path))} start="0s" duration="{dur}" '
                   f'hasVideo="{int(video)}" hasAudio="{int(audio)}" format="r1">'
                   f'<media-rep kind="original-media" src={quoteattr(url(path))}/></asset>')
    name = quoteattr(f"{edl['show']} v{version}")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.9">\n'
            f"<resources>{''.join(res)}</resources>\n"
            f'<library><event name={quoteattr(edl["show"])}><project name={name}>'
            f'<sequence format="r1" duration="{_t(total, rate)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" '
            f'audioRate="48k"><spine>\n' + "\n".join(lines) + "\n</spine></sequence></project></event></library>\n"
            "</fcpxml>\n")


def build(show_dir, output, version=1):
    edl = load_edl(show_dir)
    segments, cards, overlays, total = plan(edl, show_dir)
    layout = layout_json(edl, version, segments, cards, overlays, total)
    inputs, graph = filtergraph(edl, show_dir, segments, overlays, total)
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "a1.wav")
        mastered_mix(edl, show_dir, segments, total, wav)
        codec = encode(inputs, graph, wav, output, edl["fps"], total)
    with open(os.path.join(show_dir, "layout.json"), "w") as f:
        json.dump(layout, f, indent=1)
    with open(os.path.join(show_dir, "timeline.fcpxml"), "w") as f:
        f.write(fcpxml(edl, show_dir, segments, cards, overlays, total, version))
    return {"timeline": layout["timeline"], "file": output, "frames": total,
            "seconds": round(total / fps_value(edl["fps"]), 2), "encoder": codec}


def main(argv):
    if not argv or "--output" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    show_dir = argv[0]
    output = argv[argv.index("--output") + 1]
    version = int(argv[argv.index("--version") + 1]) if "--version" in argv else 1
    try:
        result = build(show_dir, output, version)
    except (RuntimeError, FileNotFoundError, KeyError, subprocess.CalledProcessError) as e:
        print(f"build_ffmpeg: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
