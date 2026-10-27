# Multi-clip shows and separate camera feeds (v0.2)

## What the TriCaster actually produces (checked on the NAS, 2026-10-21)

- **How exports land:** the TriCaster exports clips straight into `Shows/` as the NAS user `tricaster`. The operator types the name, `InFocus Show MM-DD-YYYY.mov`, where the date is the **airing** date. A second or third export with the same name gets ` (1)`, ` (2)` added.
  - **These suffixes are only name collisions.** They don't tell you the camera or the take.
- **No timing metadata:** there's no timecode and no creation time in the files. The file's mtime is when the *export* finished, not when it was recorded.
- **Format:** every HD export is 1920×1080 H.264 59.94 with PCM audio. Old SD DV captures (720×480) still sit in the folder.
- **Other things in the folder:** someone's `…EDITED.mp4`, a `.prproj`, and an old untitled `InFocus Show.mov`.
- **Real days:**
  - 10-21 had one file.
  - 10-22 has two (151 s and 169 s, two separate takes).
  - 08-26 has three, one of which is byte-identical in size to 08-25's file, so it was a re-export of the wrong show.
- **What's coming:** with three cameras recorded separately (TriCaster ISO), one recording gives three files of the same length, one per camera. Two recordings give six files.

So nothing can be trusted from names or times. **The editor works out the structure from the content.**

## Detection (editor worker: `editor/recordings.py`)

The steps below run on every file chosen for the job (1–12):

1. **Filter:** drop anything that isn't 1080p video (SD gets a flagged decision), and drop byte-identical duplicates (same size plus the SHA-256 of the first and last 8 MB).
2. **Fingerprint each file:**
   - its duration
   - an 8 kHz mono audio envelope
   - a picture profile: classify one frame every 2 s with `analyze.py`'s camera classifier into M, L, R, FS, BLACK or OTHER, then take the class histogram and the cut rate
3. **Group simultaneous files.** Cross-correlate every pair of envelopes. A strong peak (above the confidence ratio threshold) means the two files were recorded at the same time, and the peak position gives the offset. Connected groups are **recordings**.
4. **Within a recording:**
   - **One file:** a `program` recording (a switched feed with burned-in graphics, as today).
   - **Several files:** a file with several camera classes, FS frames or frequent cuts is the `program` feed. Every other file is a camera named by its majority class:
     - `M`: two-shot
     - `L`, `R`: side sets with the TV

     With both a program feed and camera feeds, the camera feeds are the picture (clean) and the program feed's cuts are a hint for the switching plan. Two files with the same camera class, or a camera with no clear majority, are flagged and resolved by the share of frames in each class.
5. **Order the recordings** by export finish time (mtime). The skill reorders segments by the script anyway.
6. **Write** `show/recordings.json`:

```jsonc
{ "fps": "60000/1001",
  "recordings": [
    { "id": "r1", "kind": "program" | "multicam", "frames": 9100, "confidence": 0.93,
      "files": [ { "path": "<job>/src/02.mov", "role": "program" | "M" | "L" | "R", "offsetFrames": 0 } ] } ],
  "skipped": [ { "path": "...", "reason": "duplicate of src/01.mov" } ] }
```

   - **Offsets:** in 59.94 frames, relative to each recording's common start (the first frame every file in it covers).
   - **Joins:** program recordings are never joined into one long file. The skill reads each recording separately.
7. **Overrides:** a producer's role for a file in the Portal (anything but `auto`) replaces detection for that file. An `ignore` role drops the file. The one exception: when a camera role is clearly contradicted by the picture (the feed shows that camera on under 5% of its camera frames and another one on 60% or more, e.g. L and R swapped), the picture's camera is used and recorded as a decision, unless that would put two files on one camera.
8. **Review triggers:** low-confidence grouping or role detection sets `needsReview`, with the reason as a decision.

## Portal

- **Choosing files:**
  - Up to 12 files, from `Shows/` plus one level of subfolders; a folder can be selected whole.
  - **Preselection:** files named `InFocus Show <airing date>` with an optional ` (n)` suffix, ordered by mtime.
  - **Never preselected:**
    - names containing `EDITED`
    - non-video files (`.prproj` etc.)
    - the untitled `InFocus Show.mov`
    - any file whose size exactly equals a file for a *different* date, with the note "Same size as <other file>: probably a copy of another show".
- **Role per file:** defaults to **Auto (the editor works it out)**. The options are Program, Camera M (two-shot), Camera L, Camera R and Ignore.
  - There's no camera-name map; TriCaster names don't carry it. The planned `cameraMap` column and its migration are dropped.
- **Brief:** `brief.sources = [{ path, role: "auto"|"program"|"M"|"L"|"R"|"ignore", order }]` plus the legacy `sourcePaths`. `downloads` follows the same order.
  - **Storage:** `ShowEditJob.sourcePaths` is JSON. Old string arrays mean `auto`. No migration.
- **Auto-start:** groups every preselectable file for the next show date and waits until all of them have been unchanged for 10 minutes. With several files it waits an extra 10 minutes after the newest one appears, because TriCaster exports arrive one after another.
- **After an edit:** the panel shows the detected structure from the job's `decisions`, e.g. "2 recordings: r1 multicam (M, L, R), r2 multicam (M, L, R)".

## Skill

- **Inputs:** `brief.recordings` points at `show/recordings.json`. The skill analyzes each recording into `analysis/<recording id>/`.
  - **Program recordings:** `analyze.py <program file> <show dir> --out analysis/<id>`, as before.
  - **Multicam recordings:** `analyze_cameras.py <show dir> <recordings.json> <id>` analyzes all the camera files together on the recording's timeline and writes `multicam.md`/`multicam.json`. Per feed, it reports:
    - the camera class of every shot run (does the L feed show the L set and its TV?)
    - frozen stretches

    Per speech run, it reports:
    - the words (whisper, on the M file's sound)
    - which feed's own sound is loudest
    - how much each anchor's mouth moves, from Apple Vision face landmarks (`mouth_video.swift`, compiled on first use)
    - the side that seems to be reading

    Its contact sheets show M | L | R side by side.
  - **For the worker:** `analyze.classify_frames(path, every_s=2.0, package=None) -> list[str]` returns the camera class (`M`, `L`, `R`, `FS`, `BLACK`, `OTHER`) of one frame every `every_s` seconds. The package folder defaults to `$INFOCUS_PACKAGE`. It takes about 4 s for an 8-minute file.
- **`edl.json`:**
  - **Top level:** `"recordings"`, copied from `recordings.json`.
  - **Takes:** each is `{ "kind": "source", "recording": "r1", "cam": "M", "in", "out" }`, with frames on that recording's timeline.
  - **Overlays:** each also names its `recording`.
  - **Older shows:** an edl with only `"source"` is one program recording, `r1`. Format and example: `references/edl.md`.
- **Takes:** the best take of each segment may come from any recording. The last complete clean read wins (house rule 13). A take never spans two recordings.
- **Switching** (multicam recordings): SKILL.md step 3b, and house rules 14–17 in `house-rules.example.md`:
  - The open, throws and close are on M.
  - Each announcement is on the side set of the anchor reading it, with its card on the TV.
  - **Who is reading:**
    1. mouth movement
    2. then the loudest feed
    3. then the script order in `brief.announcements`

    When the recording also has a program file, its cuts are the starting point.
  - Cut on sentence boundaries, never mid-word, with at least about 2 s per shot.
  - There are no burned-in graphics to cover. Name bars or the dual third go on at each anchor's first M shot, and the Follow card once in the close.
- **`build_ffmpeg.py`:**
  - **Picture:** each take reads its recording's file at `in + offsetFrames`: the program file, or the file for the take's `cam`.
  - **Overlays:** they land on the take of their own recording.
  - **Sound:** `master_audio.py` takes each take's sound from its recording's M file, or else its program file.
- **`verify_render.py`:**
  - **Every recording:** rules 1 and 5 (intro and outro), 6 and 7.
  - **Program recordings only**, each with its own analysis:
    - rules 2 and 3, when there are baked overlays
    - rule 4
    - the switcher-intro part of rule 5
  - **New:**
    - **8:** every L/R take shows its side set with the TV on at least 90% of its frames, and has a card.
    - **9 (multicam):** no shot (consecutive takes on one camera) is under 1.5 s.
    - **10 (multicam):** for each camera, GCC-PHAT compares the render's sound with that camera file's own sound at the same frames. A lag over 1 frame fails, and means the offsets in `recordings.json` are wrong.
    - **11:** every take lies inside its recording.

## Fixtures and tests

- **10-22:** two takes, single feed. These are real files; download them into `~/InFocusEditor/fixtures/2026-10-22/` for detection tests.
- **Pseudo-ISO 10-21** (`~/InFocusEditor/fixtures/2026-10-21-iso/`, built by `build_iso.py` there from the program recording):
  - **The files:** six TriCaster-named `.mov` files, `InFocus Show 10-21-2026.mov` through ` (5)`. Each feed shows its camera's frames while the switcher had that camera live and holds a frame elsewhere. It carries the program's sound, lined up with its picture.
  - **Two recordings, no overlap,** split at program frame 6700, between the throw and the close:

    | | M | L | R |
    |---|---|---|---|
    | r1 | program 0–6700 | 37 frames of pre-roll + program 0–6690 | program 23–6700 |
    | r2 | program 6711–8656 | program 6700–8640 | program 6729–8656 |

    The offsets are r1 M 23 / L 60 / R 0 and r2 M 18 / L 29 / R 0.
  - **`truth.json`:** the `recordings.json` the worker should detect, plus the switcher's real camera cuts on each recording's timeline.
  - **`show/`:** the hand-written switching plan (the real 10-21 cuts), with mastered audio.
  - **`golden/claim.json` + `expected.json`:** the six files shuffled, with roles `auto`.
  - **Limits:** the pseudo M feed still carries the switcher's dual third, logo bug and Follow card, and L and R are frozen in r2. Real ISO feeds have neither problem.
- **Skill tests:**
  - **`tests/test_skill_multicam.py`, synthetic:**
    - multi-recording, multi-camera builds read each camera at `in + offset`
    - overlays land on their own recording's frames
    - `classify_frames`
    - rules 8–11
    - the Vision mouth tool runs
    - cut matching
  - **Fixture tests:**
    - the hand plan builds and passes verify, with every camera's sync really cross-checked
    - its cuts land within ±1 s of the switcher's, with the same cameras
  - **Golden:** `tests/test_golden.py::test_golden_multicam_switching_matches_the_switcher` runs the real agent on `golden/claim.json`. It must find both recordings with the right roles, keep offsets within 1 frame, match every switcher cut within ±1 s with the same cameras, and pass verify.
- **Worker detection tests** (the worker's side):
  - 3 feeds with known offsets, recovered within 1 frame
  - a weak-correlation file, flagged
  - a duplicate, skipped
  - an SD file, skipped
  - a mixed program-plus-ISO recording with program detection
  - 2 recordings × 3 cameras grouped correctly
- **Real TriCaster ISO footage:** becomes the main fixture as soon as it exists.
