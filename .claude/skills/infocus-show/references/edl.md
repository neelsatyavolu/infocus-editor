# edl.json: the show's single source of truth

One file per show at `<show_dir>/edl.json`. Every script reads it; nothing else holds edit decisions or graphic text.
A full worked example (the 10-06-2026 show) is in `example-edl.json`.

```jsonc
{
  "show": "InFocus Show 10-06-2026",          // the show's name; layout.json calls the edit "<show> vN"
  "fps": "60000/1001",                         // the recording's frame rate = the timeline's. All frame numbers use it.
  "source": "/path/to/job/source.mov",          // brief.source
  "package": "/path/to/InFocus 2026 Package",  // brief.packageDir; {PACKAGE} below expands to it
  "audio": { "peak_dbfs": -6, "dialogue_lufs": -16, "channels": [0, 1] },   // master_audio.py; channels = mic channels in the recording
  "segments": [                                // in program order
    { "id": "intro", "kind": "file", "path": "{PACKAGE}/03 Intro & Outro/InFocus Intro 2026.mp4" },
    { "id": "open", "kind": "source", "cam": "M", "in": 3524, "out": 4684,
      "note": "Take 2 (take 1 was a false start)" },          // why: for the README and the next person
    { "id": "college_visits", "kind": "source", "cam": "L", "in": 6260, "out": 7494,
      "card": { "title": "College visits tomorrow",
                "points": ["Michigan State 9:05AM", "Northeastern University, 9:50 AM"],
                "image": "/path/to/qr.png",                  // optional QR code (announcement_media.py), shown on the right
                "kicker": "Announcements" } },                // optional, default "Announcements"
    { "id": "package", "kind": "placeholder", "seconds": 5, "label": "Spirit Week Day 1 Recap",
      "note": "Package goes here (not in the recording)" },    // a slate is rendered for it
    { "id": "pkg_ice", "kind": "file", "path": "/path/to/job/packages/01-ice.mp4", "audio": "match" },   // a package you do have
                                               // "audio": "match" = loudness-matched to the anchors + limiter (use for packages;
                                               // without it a file gets one static gain to the peak ceiling, right for intro/outro music)
    { "id": "outro", "kind": "file", "path": "{PACKAGE}/03 Intro & Outro/InFocus Outro 2026.mp4" }
  ],
  "overlays": [                                // V4, placed by source frame
    { "id": "dual_anchors", "at": 3620, "fps": "30000/1001", "covers": [3624, 3931],
      "render": { "type": "dual", "name1": "Abby Smith", "title1": "InFocus Anchor",
                  "name2": "Otto Lee", "title2": "InFocus Anchor", "duration": 5300 } },
    { "id": "lower_sage", "at": 1234, "covers": [1236, 1550],
      "render": { "type": "lower", "name": "Sage Park", "title": "InFocus Reporter", "duration": 5300 } },
    { "id": "follow", "at": 27275, "covers": [27277, 27682],
      "file": "{PACKAGE}/08 Follow InFocus/InFocus Follow Us.mov" }
  ]
}
```

## Several recordings and camera feeds

When the show came in more than one recording, or as separate camera feeds, the file carries `"recordings"` (copied from the worker's `show/recordings.json`) instead of `"source"`, and every take and overlay names its recording:

```jsonc
{
  "show": "InFocus Show 10-21-2026", "fps": "60000/1001", "package": "/path/to/InFocus 2026 Package",
  "recordings": [
    { "id": "r1", "kind": "multicam", "frames": 6667, "files": [
        { "path": "/path/to/job/src/01.mov", "role": "M", "offsetFrames": 23 },
        { "path": "/path/to/job/src/02.mov", "role": "L", "offsetFrames": 60 },
        { "path": "/path/to/job/src/03.mov", "role": "R", "offsetFrames": 0 } ] },
    { "id": "r2", "kind": "program", "frames": 9100, "files": [
        { "path": "/path/to/job/src/04.mov", "role": "program", "offsetFrames": 0 } ] } ],
  "segments": [
    { "id": "intro", "kind": "file", "path": "{PACKAGE}/03 Intro & Outro/InFocus Intro 2026.mp4" },
    { "id": "open", "kind": "source", "recording": "r1", "cam": "M", "in": 1135, "out": 1867 },
    { "id": "chess_club", "kind": "source", "recording": "r1", "cam": "R", "in": 1867, "out": 3119,
      "card": { "title": "Chess Club", "points": ["After school today", "Room 100"] } },
    { "id": "close", "kind": "source", "recording": "r2", "cam": "M", "in": 6836, "out": 8150 },
    { "id": "outro", "kind": "file", "path": "{PACKAGE}/03 Intro & Outro/InFocus Outro 2026.mp4" }
  ],
  "overlays": [
    { "id": "dual_anchors", "recording": "r1", "at": 1187, "fps": "30000/1001",
      "render": { "type": "dual", "name1": "Abby Smith", "title1": "InFocus Anchor",
                  "name2": "Otto Lee", "title2": "InFocus Anchor", "duration": 8550 } },
    { "id": "follow", "recording": "r2", "at": 7116, "covers": [7118, 7522],
      "file": "{PACKAGE}/08 Follow InFocus/InFocus Follow Us.mov" }
  ]
}
```

- **Frames:** `in`/`out`/`at` are frames of the named recording's timeline. Frame `t` of a recording is frame `t + offsetFrames` of each of its files.
- **Multicam takes:** the picture is the file whose `role` is the take's `cam`. Each take is one shot of the switching plan.
- **Program takes:** the picture is the recording's `program` file, and `cam` is the camera the analysis saw.
- **Sound:** every take's sound comes from its recording's M file, or else its program file.
- **One recording per take:** a take never spans two recordings.
- **Covers:** only program recordings have burned-in graphics, so only their overlays carry `covers`.

## Rules

- **Frames:** `in` is the first frame used and `out` is exclusive, both in the recording's own frame numbers (as printed by `analyze.py` and `sheet.py`). The segment length is `out - in`.
- **`cam`** (`M`, `L` or `R`) is needed only for segments with a `card`. It picks which TV screen the card goes on.
- **Cards:** a card spans its whole segment and starts and ends on the TV Standby frame. If the TV should only show a card for part of a camera run, split that run into two segments (contiguous source frames give a seamless picture), one with the card and one without. A source segment with no card keeps whatever the switcher put on the TV. On a side camera, that means you must give it a card, or else the old filmed card shows.
- **Splitting a live run** (for example at each switcher cut, so each anchor's mic can be levelled on its own) is invisible in the picture. Do it whenever a run alternates anchors.
- **Overlays:**
  - `at` must fall inside a chosen source segment.
  - `render` takes any `graphics.html` config (`dual`, `lower`, `follow`, …), rendered at `fps` (default 29.97).
  - `file` uses a ready-made clip, e.g. the roster lower thirds in `{PACKAGE}/05 Lower Thirds/Roster (ProRes)/<Name> - Lower Third.mov`.
  - `covers` is the old baked-in overlay's [first, last] visible frame. It's used by `check_render.py` and is good documentation.
- **Text:** `|` in a card title forces a line break. Five points max per card; more reads as a slide, so split the card.
- **`chapter`** (optional, on any segment): starts a new chapter with this title. The worker makes chapters for the Portal's player and the YouTube description by itself: Intro, Welcome (the open), each announcement's card title, "Package: <title>", Sign-off and Outro; other segments continue the chapter before them. Set it only when that would be wrong, e.g. `"chapter": "Sports"` on a segment that isn't an announcement.
- **`toss_said`** (on a package `file` segment): what the anchor said in the throw, as clean sentences. The worker sends it to the Portal for the YouTube description.
- **`script`** (optional, on a card): the index of the `brief.announcements` paragraph the card was made from. Set it on every card you write or match; the worker sends each card with its script text to the Portal, which hands them to later shows as `brief.pastCards`.
- **Paths:** use absolute paths from `brief.json`. `{PACKAGE}` expands to `"package"` (copy `brief.packageDir` there), and `~` is expanded.
- **Placeholders:** each `placeholder` segment gets a slate rendered automatically. Swap in a `file` segment once the package exists.
