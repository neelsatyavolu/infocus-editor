# Hard-won details (read when something looks off)

## The recording
- The switcher records the **program output**, with every graphic burned in: sets, TV cards, name bars and Follow card. You can't remove graphics, only **replace** them:
  - TV cards get a fresh card fitted exactly over the screen.
  - Name bars and the Follow card get a fresh one that fully covers the old one.
- **Side cameras are the whole set frame.** The 2026 Left and Right sets fill the frame unzoomed, so the TV screens sit at fixed pixels (`SCREENS` in `common.py`: L 55,112 1124×630 and R 741,108 1122×633). If a show's side shot ever looks reframed, re-measure with `sheet.py --crop` before trusting them.
- **TVs left on the blue key.** A show may be filmed with the side TVs unfilled, showing the sets' flat blue screen (`#0047BB`, `tv_key.py`) instead of the TV Standby. The analysis reads it like the standby (the side set, no card filmed), so every L/R take gets a written card that covers the blue. The foreground keys the blue out of the anchor's soft edges and pulls its spill, only on frames where the TV shows it. The finished show never shows the blue.
- **Anchors cross the TV.** On one show an anchor's shoulder sat over the right TV's lower-left corner in almost every frame. That's why the foreground pass exists; never skip it for L/R card segments.
- **SD vs HD captures:**
  - There may be an SD DV capture (720×480 29.97, anamorphic 16:9) and an HD file (1080p 59.94) of the same show. Always edit from the HD one; ask for it if you only have SD.
  - In 10-06 the audio was identical, but the HD picture led the SD picture by 2 SD frames (HD frame = 2 × SD − 4).
  - Measure switcher cuts directly in whichever file you edit from.
- **Baked overlays are 29.97 renders frame-doubled** inside a 59.94 recording, so they change on every other frame.
  - Render the covering overlay at 29.97 too, and start it so its frames change in phase with the old one. Starting 2 HD frames before the old one's first visible frame matches the same template exactly. Starting one 29.97 frame (2 HD frames) earlier still keeps it ahead of the old one.
  - Check by eye: `check_render.py` makes `covers_<id>.jpg`.
- **The fresh plates cover the old ones:** the same template renders plates the same size or slightly bigger. Check this per show, though: a longer old name, or a typo the anchor read out, can make an old plate wider. Measure both (old: ink/green bounding box; new: alpha bounding box). If the old plate is bigger, raise `render.scale` slightly.

## Several recordings and camera feeds
- **Names and times mean nothing:** TriCaster exports are all called `InFocus Show <date>.mov`, with ` (1)`, ` (2)` for name collisions, and carry no timecode. The worker works out which files were recorded together, and each file's role, from the sound and the picture (`recordings.json`). Trust that over names.
- **Offsets are per file:** a recording's frame `t` is frame `t + offsetFrames` of each file. Use `sheet.py` on the file itself with the offset added when you check a multicam cut.
- **Who is reading:** with separate camera mics the reader's feed is loudest. If every feed carries the same sound, the mic tells you nothing, and mouth movement (`multicam.md`) and the script order decide.
- **Frozen or wrong feeds:** a feed that holds one picture for a long stretch (`frozen` in `multicam.md`) has no live camera there. Don't cut to it.
- **Sync:** `verify_render.py` rule 10 compares the render's sound with each camera file's own sound. A failure means the recording's offsets are off for that camera. That's a review item, not something to fix by nudging `in`/`out`.

## Picking takes
- Whisper drifts on long files. `analyze.py` transcribes in 2-minute chunks, but word times still wander by ±0.5 s. Put cut points in silences from `speech_runs_s` and confirm with `sheet.py`.
- Repeated identical lines ("Who's on teleprompter?" ×50, "VIKINGS" ×10) are hallucinations over silence, not speech.
- Crew chatter between takes ("start from the top", "are they recording", "camera three") is the clearest restart marker.
- When the anchor restarts, the **last complete** read is usually the keeper. If none is complete, splice at a sentence boundary in a pause, on the same camera, and check that the anchor's position matches.
- Watch for:
  - a 0.3–0.5 s full-screen card flash (the operator's mis-cut)
  - the outro firing early
  - anchors relaxing (head down, hands moving) right after a line, which sets your out point

## Building and rendering (`build_ffmpeg.py`)
- **Frame rules** (the same an editing timeline uses):
  - A segment's `out` is **exclusive**: it lasts `out - in` frames.
  - A file at another rate is conformed by duration and rounded down: a 828-frame 60 fps intro lasts 827 frames at 59.94, and a 153-frame 29.97 outro lasts 306. A 29.97 clip shows each of its frames twice.
  - An overlay starts at its `at` frame and runs for the clip's whole (conformed) length.
- **New file name for every remade clip and every render version** (`… v3.mp4`, `hold_f6108_29f.mov`). A reviewer, a player or a cache can't then confuse an old file with its replacement.
- **Encoder:** the render uses VideoToolbox H.264 (libx264 if VideoToolbox isn't available).
  - Run nothing else heavy alongside it, such as whisper or `analyze.py`.
  - A Mac that sleeps mid-render leaves a broken file. Re-run it, and always ffprobe the result for a video and an audio stream.
- **`timeline.fcpxml`:** written next to `layout.json`, so a person can open the same edit in an editing app (File ▸ Import ▸ Timeline) and fix it by hand. Card positions there are approximate; the render is the reference.

## Audio
- **Package music has inter-sample peaks.** With a plain limiter, dense music's true peak sat 1.6 dB over a −6 sample-peak ceiling. `master_audio.py` limits 4× oversampled and sets each segment's final gain on the 4× true peak; AAC still adds about 0.2 dB.
- **Peak normalization alone spreads loudness about 9 dB,** because each clip has a few 10–120 ms plosives or laughs 4–9 dB over its typical peaks. That's why `master_audio.py` levels loudness first and limits after.
- **Packages arrive already mixed, often clipping:** one peaked at +0.2 dBFS at −19 LUFS. Peak-normalizing it would leave it about 6 dB under the anchors, so use `"audio": "match"`.
- **Low crest factor:** a throw or package can sit under −6 after the loudness trim. The final peak gain lifts it (up to +6 dB), so its loudness ends up a little over −16 LUFS; `verify_render.py` allows for exactly that lift.
- **Package lyrics:** listen to (or transcribe) package music. One package had explicit lyrics, which is a school-broadcast issue; flag it.
- **Limiter latency:** ffmpeg `alimiter` needs `latency=true`, or it shifts the audio 3 ms. WAVs are written one frame long, and `build_ffmpeg.py` cuts each to its segment's exact picture length.
- **zsh:** it treats `$m:s` as a modifier, so write ffmpeg filter strings in Python, not in shell loops.

## Package rules (the producer's decisions; don't undo)
- Name plates are square. Use Soft White `#ECEFEA`, never pure white.
- The TVs idle on `09 TV Standby`, never on the blue key. Announcements start and end on that exact frame.
- The Follow card sits top left. The outro uses the original music.
- Package graphics go on at their own size and timing, always. A graphic is never made smaller or changed to get past a check (the studio check ignores the edit's own overlays).
- Rule #1 from `_Working Files/NOTES.md`: change the *source* (`edl.json`, `graphics.html`), then re-render. Never hand-edit a render.
