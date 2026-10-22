# Job folder contract (worker ↔ skill)

The worker (`editor/`) and the autonomous skill (`.claude/skills/infocus-show/`) only talk through files in the job folder and through `verify_render.py`. Neither imports the other.

```
<work_root>/<showDate>-<jobId>/
  brief.json          worker → skill (rewritten for every run, including fixes)
  status.json         skill → worker (progress; written by scripts/progress.py)
  result.json         skill → worker (written at the end of every run)
  src/01.mov …        the downloaded source files (never joined; see show/recordings.json)
  packages/01-<name>  the queued packages, in queue order
  show/               the skill's show folder (edl.json, Graphics/, analysis/, check/, layout.json …)
  out/                renders and posters, one pair per version
  logs/               worker only; the skill never writes here
    editor.log                    the job log: every runner step, a "----- run <n> (<kind>, v<version>) -----" header per run
    agent-<NN>-<backend>.jsonl    one agent session's raw events, numbered per job folder (01, 02, … across runs)
    agent-<NN>-<backend>.stderr.log   a CLI agent's (Codex, Grok) stderr
    debug-<version>.tar.gz        the debug bundle uploaded to the Portal after each run
```

## logs/

- **Written by the worker,** never by the skill. Everything is scrubbed of tokens on the way to disk.
- **The debug bundle** holds `logs/` (minus older bundles), `brief.json`, `result.json`, `status.json`, `show/edl.json`, `show/recordings.json`, `show/layout.json`, `show/check/verify.json`, and `show/analysis/**/report.md` plus the JSON files under `show/analysis` up to 5 MB each.
  - No media (`.mov`, `.mp4`, `.wav`, `.png`, `.jpg`, …).
  - Capped at 50 MB: past that, the middle of each transcript (and of the job log) is cut and a marker line put in its place.
  - The copies in the bundle are scrubbed too; the job's own files are left as they are.
  - The worker uploads it to the Portal's `upload-targets` `log` path and reports that path as `debugLogPath` (see `portal-api.md`).

## brief.json

```jsonc
{
  "jobId": "…",
  "kind": "edit",                  // or "fix"
  "showDate": "2026-10-21",        // airing date (the show is named "InFocus Show MM-DD-YYYY" from it)
  "filmedOn": "2026-10-20",        // or null
  "version": 1,                    // the output version this run produces
  "recordings": "<job_dir>/show/recordings.json", // what the files are: see multicam.md
  "source": "<job_dir>/src/01.mov", // only when the job is exactly one program file (older skills)
  "anchors": ["First Last"],       // the spelling to use on name bars and dual thirds
  "announcements": ["…"],          // the script's announcement text (ground truth for TV cards)
  "pastCards": [{ "showDate": "…", "title": "…", "points": ["…"], "script": "…" }],  // earlier shows' cards: the style for written cards
  "roster": ["…"],                 // the class's full names: name bars, and who's on air
  "packages": [{ "title": "…", "localPath": "<job_dir>/packages/01-x.mp4", "toss": "…" }],
  "note": null,                    // the producer's instructions for this edit, from Start or Redo (quoted data about this edit only)
  "fix": null,                     // kind "fix": { "text": "…", "remember": false }
  "showDir": "<job_dir>/show",
  "output": "<job_dir>/out/InFocus Show 2026-10-21 v1.mp4",
  "poster": "<job_dir>/out/InFocus Show 2026-10-21 v1.poster.jpg",
  "packageDir": "<the InFocus 2026 Package folder>",
  "houseRules": "<data_dir>/house-rules.md"
}
```

- **Output names:** every version gets new output names, so a reviewer never sees a cached older file.
- **Paths:** all paths are absolute.

## status.json

`scripts/progress.py <stage> "<message>"` writes `{ "stage", "message", "at" }` atomically (temp file, then rename). `stage` is one of the Portal stages: `analyze | graphics | audio | build | render | check | fix`. The worker itself reports `download` and `upload`.

## result.json

```jsonc
{
  "output": "<brief.output>",
  "poster": "<brief.poster>",
  "decisions": [{ "kind": "name|typo|take|package|fixup|card_written|card_mismatch|lyrics|other", "detail": "…", "flagged": false }],
  "checks": {
    "coversClean": true,           // == check/verify.json "pass"
    "loudness": [{ "segment": "open", "lufs": -19.5, "peak": -6.0, "ok": true }],
    "failures": []                 // verify.json failures, if any are left
  },
  "needsReview": false,            // true if any decision is flagged or any check failed
  "reply": null,                   // kind "fix": one paragraph saying what changed
  "abort": null                    // set to a plain reason when the recording can't be edited
                                   // (e.g. "This doesn't look like a show recording: no anchor shot")
}
```

The JSON Schema is `tests/result.schema.json`.

## verify_render.py

```
uv run --with numpy --with opencv-python --with pillow --with soundfile \
  python .claude/skills/infocus-show/scripts/verify_render.py <showDir> --render <output>
```

- **Output:** writes `<showDir>/check/verify.json` as `{ "pass": bool, "failures": [{ "rule", "segment"?, "overlay"?, "frames"?: [a, b], "detail" }], "loudness": [...] }`.
- **Exit codes:** 0 pass, 1 fail, 2 could not run.

The worker runs it after every agent session and doesn't trust the agent's own `coversClean`.
