# Anchor Recomposite + Dual-Third Poster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opt-in re-compositing of every take onto the theme's clean virtual set, with the anchors matted and re-seated; plus a poster picked from the dual third's fully-on window by face quality.

**Architecture:**
- **The new step:** `recomposite.py` turns each source take into a finished picture clip (`Recomposite/<id> - Recomposite.mov`): set plate → fresh card → anchors → table front panel.
- **Where the code comes from:** the prototype on branch `experiment/anchor-reseat` (commit `9552c69`), split into pure modules:
  - `virtual_set.py`: set images and geometry
  - `seat.py`: placement
  - `anchor_alpha.py`: per-frame matte
- **Speed:** frames are rendered in parallel worker processes.
- **Build:** `build_ffmpeg.py` reads those clips instead of the recording plus the card and foreground layers.
- **Checks:** `verify_render.py` gets a mixed-set rule.
- **Poster:** a Vision landmark tool plus `poster_pick.py` choose the poster frame.
- **Portal:** the opt-in travels as `brief.recomposite`.

**Tech Stack:** Python 3 scripts run with `uv run --with numpy --with opencv-python`, Swift + Apple Vision helpers compiled on first use, ffmpeg, pytest. Portal: Next.js + Prisma + zod + vitest (`~/Documents/GitHub/infocus-packages`).

**Spec:** `docs/superpowers/specs/2026-10-29-anchor-recomposite-design.md`

## Global Constraints

- **Opt-in for baked-set recordings:** `brief.recomposite` (Portal checkbox), the producer's note asking for it, or a themed show. When it's off, today's output is byte-for-byte unchanged in behaviour.
- **Green screen is out of scope** for this build. Matte selection is a function with one branch (`"baked"`), so a `"green"` branch can be added later.
- **Layer order:** set PNG → fresh card on the TV (L/R) → anchors → table front panel below `DESK_LIP` → package overlays.
- **One transform per take** (scale `s`, offset `tx, ty`). Scale is clamped to [0.8, 1.25], with 60 px of headroom.
- **Placement:** waist on `DESK_BACK` (+4 px tuck). A frame-edge cut is never pulled into view. Two-shots cut at the bottom sit low behind the front panel; record an `other` decision, not flagged.
- **Shared transform:** takes from the same recording and camera whose transforms are within 8 px and 0.02 scale of their median all use the median.
- **Fallback when a take fails its sanity checks:**
  - Output theme == recorded theme: the original picture, with a flagged `other` decision.
  - Output theme ≠ recorded theme: keep the best matte, with a flagged `other` decision naming the take.
- **Time guard:** a pass budget of `RECOMPOSITE_BUDGET_S = 1800`. If the projected time is over budget:
  - Same theme: the whole show falls back (never part of it), flagged.
  - Different theme: continue, flagged.
- **Speed target:** the pass takes at most 2.5× the show's run time on the studio Mac (an 8-minute show adds ≤ 20 minutes).
- **Skill rules stay:**
  - Package graphics are never changed.
  - Re-compositing happens only through `recomposite.py` when `edl.json` has `"recomposite": true`, and never to get past a check.
- **Code style:** match the skill scripts. No type annotations, short docstrings, constants at the top, `common.py` helpers. Tests use synthetic data, like `tests/test_skill_themes.py`.
- **Git:**
  - Work on branch `feat/anchor-recomposite` (editor) and `feat/recomposite-checkbox` (infocus-packages).
  - Commit messages follow the `<type>: <description>` format.
  - Don't touch infocus-packages' uncommitted `docs/SUBDOMAINS.md` or `docs/knowledge/automatic-editing.md`.

## Review Focus

1. **A take filmed on the Halloween set, output on the regular look:**
   - Expected: the recorded theme is detected as `halloween`, the plate difference uses the Halloween plate, and the output goes on the 2026 set.
   - Covered by `test_recorded_theme_picks_the_matching_set` in Task 1.
2. **A burned-in name bar in a program take:**
   - Expected: it must not travel with the moved anchor.
   - Covered by `test_old_graphic_box_is_cut_from_the_alpha` in Task 3.
3. **Several takes from the same camera with slightly different measurements:**
   - Expected: the anchors don't jump between cuts.
   - Covered by `test_close_transforms_share_their_median` in Task 2.
4. **A theme installed without Left/Right sets** (an older Mac's `Themes/Halloween/` missing them):
   - Expected: a clear error before any rendering, never a crash mid-render.
   - Covered by `test_missing_set_png_is_a_clear_error` in Task 1.
5. **A recomposited L/R take on the Halloween set:**
   - Expected: verify rule 8 still recognises it as an L/R studio frame.
   - Covered by `test_recomposited_side_take_still_reads_as_its_set` in Task 7.

---

## File map

| File | Responsibility |
|---|---|
| `.claude/skills/infocus-show/scripts/virtual_set.py` (new) | Set PNG per cam and theme, table geometry, seats, recorded-theme detection |
| `.claude/skills/infocus-show/scripts/seat.py` (new) | Waists, edge touches, `fit`, shared-transform snapping |
| `.claude/skills/infocus-show/scripts/anchor_alpha.py` (new) | Per-frame alpha from the Vision matte and plate; unmixing; temporal steadying; cutting out old graphics |
| `.claude/skills/infocus-show/scripts/anchor_matte.swift`, `face_boxes.swift`, `face_marks.swift` (new) | Vision helpers: instance matte, face boxes, face landmarks |
| `.claude/skills/infocus-show/scripts/recomposite.py` (new) | Per-take measure → fit → sanity → parallel render → `Recomposite/plan.json` |
| `.claude/skills/infocus-show/scripts/poster_pick.py` (new) | Candidate poster frames from the dual third's fully-on window, scored, plus a contact sheet |
| `common.py`, `build_ffmpeg.py`, `verify_render.py` (modify) | Theme sets, recomposite file names, build input switch, rule 12 |
| `SKILL.md`, `references/edl.md`, `references/gotchas.md` (modify) | When it runs, the new step, the reworded anchor rule, the poster step |
| `editor/jobfiles.py`, `docs/job-contract.md`, `docs/portal-api.md` (modify) | `brief.recomposite` |
| `tests/test_recomposite_*.py`, `tests/test_poster_pick.py` (new); `tests/test_skill_contract.py`, `tests/test_build_ffmpeg.py`, `tests/test_runner.py` (modify) | Tests |

---

### Task 0: Branch and bring the prototype over

- [ ] **Step 1:** `git checkout -b feat/anchor-recomposite main`
- [ ] **Step 2:** `git show 9552c69:experiments/anchor-reseat/reseat.py > /tmp/reseat_reference.py`, and the same for the two `.swift` files. These are reference material for Tasks 1–5, not files to commit.
- [ ] **Step 3:** Leave the untracked `experiments/` folder alone. It belongs to the experiment branch.

---

### Task 1: Theme sets and recorded-theme detection

**Files:**
- Create: `.claude/skills/infocus-show/scripts/virtual_set.py`
- Modify: `.claude/skills/infocus-show/scripts/common.py` (`THEMES`)
- Test: `tests/test_recomposite_sets.py`

**Interfaces:**
- Produces:
  - `common.THEMES[theme]["left" | "middle" | "right"]`: package-relative PNG paths.
    - Regular look: `04 Virtual Sets/InFocus Virtual Set 2026 - {Left,Middle,Right}.png`
    - Halloween: `Themes/Halloween/04 Virtual Sets/InFocus Virtual Set Halloween 2026 - {Left,Middle,Right}.png`
  - `virtual_set.set_image(cam, theme, package) -> np.ndarray` (H×W×3 uint8 RGB). Raises `RuntimeError("No <cam> set for theme '<theme>' (<path>)")` when the PNG is missing.
  - `virtual_set.geometry(cam, theme) -> dict` with keys `seats` (`{"x": [...], "y", "h"}`), `desk_back` and `desk_lip` (lists of `(x, y)` points). The values are the prototype's `SEATS` / `DESK_BACK` / `DESK_LIP`. A theme with no override of its own uses the regular look's values.
  - `virtual_set.installed_themes(package) -> list[str]`: themes whose three set PNGs all exist.
  - `virtual_set.recorded_theme(frame, mask, cam, package) -> str`: the installed theme whose `cam` set best matches `frame` where `mask` (bool, True = anchor) is False. Measured as the median of max-channel abs diff; lowest wins.

- [ ] **Step 1: Write failing tests** in `tests/test_recomposite_sets.py`, with a synthetic package in `tmp_path`:
  - `test_theme_sets_live_in_the_package`: `common.theme_file("left", {"package": "/pkg"})` ends with `"04 Virtual Sets/InFocus Virtual Set 2026 - Left.png"`, and the Halloween `"right"` path ends with `"Themes/Halloween/04 Virtual Sets/InFocus Virtual Set Halloween 2026 - Right.png"`.
  - `test_missing_set_png_is_a_clear_error`: `pytest.raises(RuntimeError, match="No L set for theme 'halloween'")`.
  - `test_recorded_theme_picks_the_matching_set`:
    - Setup: two solid-ish sets (regular grey `(120,120,120)`, Halloween orange `(200,100,30)`). The frame is the Halloween set with a `(30,30,30)` 400×600 "person" block, and `mask` is that block.
    - Expected: `"halloween"`.
  - `test_unknown_themes_geometry_falls_back_to_regular`: `geometry("M", "halloween") == geometry("M", "")`.
- [ ] **Step 2:** Run `uv run --with numpy --with opencv-python --with pillow --with pytest pytest tests/test_recomposite_sets.py -v`. Expected: FAIL (no module `virtual_set`).
- [ ] **Step 3: Implement** the `THEMES` keys and `virtual_set.py` with the signatures above. Copy the constants verbatim from the prototype.
- [ ] **Step 4:** Run the same command. Expected: PASS. Also run `pytest tests/test_skill_themes.py` and confirm it still passes.
- [ ] **Step 5: Check the real sets.**
  - Load the 2026, Halloween and Thanksgiving Left/Middle/Right PNGs from `~/Downloads/InFocus/Show Resources/*/04 Virtual Sets/`.
  - Draw `desk_back` and `desk_lip` on each (`cv2.polylines`) into the scratchpad, and view them.
  - If a theme's table differs by more than 6 px anywhere, add that theme's override to `virtual_set.GEOMETRY` and a test for it.
- [ ] **Step 6:** `git commit -m "feat(skill): theme virtual sets and recorded-set detection"`

---

### Task 2: Placement

**Files:**
- Create: `.claude/skills/infocus-show/scripts/seat.py`
- Test: `tests/test_recomposite_seat.py`

**Interfaces:**
- Consumes: `virtual_set.geometry`.
- Produces:
  - `seat.waists(alphas, face_x) -> [(cx, waist_y)]`: the prototype's `waists`.
  - `seat.edges_touched(mattes) -> {"left", "right", "top", "bottom": bool}`.
  - `seat.fit(geom, boxes, touch, alphas) -> (s, tx, ty, note)`: the prototype's `fit`, with `geom` from `geometry()` replacing the module globals. The constants `HEADROOM=60`, `WAIST_TUCK=4`, `TOL={"s":0.10,"x":30,"y":25}`, `LIP_PANEL=5` and the clamp to [0.8, 1.25] are kept.
  - `seat.share_transforms(takes) -> takes`: `takes` is a list of dicts with `recording`, `cam`, `s`, `tx`, `ty`. Returns new dicts (no mutation) where each group (same recording and cam) whose members are all within 8 px and 0.02 scale of the group median gets the median.

- [ ] **Step 1: Write failing tests:**
  - `test_waist_lands_on_the_table_back_edge`:
    - Setup: a synthetic single-anchor silhouette (a rectangle torso, rows 400–1000, cols 1300–1700), a face box `[1450, 330, 100, 130]`, cam R.
    - Expected: after `fit`, `s * 1000 + ty` is within 6 px of `edge_y(desk_back, …) + 4`.
  - `test_tight_two_shot_never_shows_its_bottom_cut`:
    - Setup: a silhouette touching the bottom rows.
    - Expected: the placed bottom row is ≥ the lip + `LIP_PANEL`.
  - `test_well_seated_shot_is_left_alone`:
    - Setup: a silhouette already at the seat.
    - Expected: `(1.0, 0.0, 0.0)`.
  - `test_close_transforms_share_their_median`:
    - Setup: three R takes at `s=1.00/1.01/0.99`, `tx=10/14/12`.
    - Expected: all three get `(1.00, 12, …)`. A fourth at `tx=60` keeps its own.
  - `test_no_faces_leaves_the_shot_alone`: `fit` with empty boxes returns `s == 1.0`, with a note containing `"no faces"`.
- [ ] **Step 2:** Run `pytest tests/test_recomposite_seat.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement by porting from `/tmp/reseat_reference.py`, without changing the behaviour.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** `git commit -m "feat(skill): anchor placement on the virtual set"`

---

### Task 3: Per-frame alpha

**Files:**
- Create: `.claude/skills/infocus-show/scripts/anchor_alpha.py`
- Test: `tests/test_recomposite_alpha.py`

**Interfaces:**
- Produces:
  - `anchor_alpha.alpha_for(frame, vis, plate, cam, chin) -> float32 H×W in 0..1`: the prototype's function. It uses `common.SCREENS`.
  - `anchor_alpha.unmix(frame, a, behind) -> float32 H×W×3`.
  - `anchor_alpha.steady(mattes)`: a generator, the prototype's 3-frame median.
  - `anchor_alpha.cut_boxes(a, boxes, pad=30) -> new array`: alpha set to 0 inside each `(x0, y0, x1, y1)` box, padded.
  - `anchor_alpha.matte_kind(recording) -> "baked"`: the only branch for now. A `recording` of `kind == "green"` raises `NotImplementedError("green-screen recordings arrive in a later phase")`.

- [ ] **Step 1: Write failing tests:**
  - `test_anchor_inside_vision_matte_is_kept_set_is_dropped`:
    - Setup: plate = a noisy grey set, frame = plate with a `(40,60,200)` blob, and `vis` = the blob's mask dilated 15 px.
    - Expected: alpha is > 0.95 at the blob centre and < 0.05 in the dilated ring, where the frame equals the plate.
  - `test_old_graphic_box_is_cut_from_the_alpha`: `cut_boxes(np.ones((1080,1920)), [(150,840,960,975)])` is 0 at `(900, 300)` (row 900, col 300) and 1 at `(500, 300)`. The input is unchanged.
  - `test_unmix_removes_the_old_set_from_edges`:
    - Setup: a pixel with `C = 0.5*F + 0.5*B`, where F is red and B is green.
    - Expected: `unmix` returns F within 2 levels.
  - `test_green_screen_is_not_built_yet`: `matte_kind({"kind": "green"})` raises `NotImplementedError`.
- [ ] **Step 2:** Run `pytest tests/test_recomposite_alpha.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement by porting from the prototype, keeping `CARD_INK=(15,17,15)`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** `git commit -m "feat(skill): anchor matte refinement"`

---

### Task 4: Vision helpers

**Files:**
- Create: `scripts/anchor_matte.swift` (copied from `anchormatte_video.swift`, with frame-index arguments), `scripts/face_boxes.swift` (copied as is), `scripts/face_marks.swift`
- Modify: `common.py` (add `ensure_tool(name)` next to `ensure_matter`)
- Test: `tests/test_recomposite_tools.py` (skipped when `swiftc` isn't on PATH)

**Interfaces:**
- Produces:
  - `common.ensure_tool(name) -> path`: compiles `scripts/<name>.swift` into `scripts/bin/<name>` on first use, the same way as `ensure_matter`.
  - `anchor_matte <in> <first_frame> <count> <keep>`: writes `count` 1920×1080 gray frames to stdout, starting at the frame index. Seek by `first_frame / fps` using the track's nominal rate, then skip samples until the presentation time reaches `first_frame`, so chunks line up exactly.
  - `face_boxes <image>...`: unchanged.
  - `face_marks <image>...`: one JSON line per image: `{"path", "faces": [{"box": [x,y,w,h], "eyes": e, "mouth": m, "yaw": y}]}`.
    - `eyes`: the mean over both eyes of eye height / eye width, from `leftEye` / `rightEye` normalized points.
    - `mouth`: the inner-lip gap / face height, as in `mouth_video.swift`.
    - `yaw`: `VNFaceObservation.yaw` in radians, or 0.

- [ ] **Step 1: Write failing test:** `test_tools_compile_and_report_no_faces_on_a_blank_frame`. It compiles all three, runs `face_marks` and `face_boxes` on a black PNG (expected: `"faces": []`), and runs `anchor_matte` on a 10-frame black clip with `0 10 2`. Expected: exactly `10*1920*1080` bytes.
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it. Expected: PASS. Add `scripts/bin/` names to the existing ignore if the skill has one (check `git check-ignore scripts/bin/personmatte_video`).
- [ ] **Step 5:** `git commit -m "feat(skill): Vision helpers for anchor matte and face landmarks"`

---

### Task 5: `recomposite.py`

**Files:**
- Create: `.claude/skills/infocus-show/scripts/recomposite.py`
- Modify: `common.py` (add `recomposite_file(show_dir, seg)` → `<show_dir>/Recomposite/<id> - Recomposite.mov`, and `recomposite_on(edl) -> bool(edl.get("recomposite"))`)
- Test: `tests/test_recomposite_plan.py`

**Interfaces:**
- Consumes: Tasks 1–4. Needs `Graphics/<id> - Monitor.mov` (the card, from `render_graphics.mjs`) for L/R takes with a card.
- Produces:
  - CLI `uv run --with numpy --with opencv-python python recomposite.py <showDir> [--only <id>] [--workers N]`. `--workers` defaults to `max(2, os.cpu_count() // 2)`.
  - `<showDir>/Recomposite/plan.json`:
    ```json
    {"theme": "", "seconds": 0, "abandoned": null,
     "takes": [{"id", "recording", "cam", "recorded_theme", "s", "tx", "ty",
                "status": "recomposited" | "fallback", "flag": null | "<sentence>", "note"}]}
    ```
  - Per take: `Recomposite/<id> - Recomposite.mov` (H.264 `-crf 12 -preset fast`, yuv420p, the show's rate, exactly `out - in` frames) and `Recomposite/<id>-before.jpg` / `-after.jpg` / `-alpha.jpg` from its middle frame.
  - Pure functions (tested):
    - `take_ok(face_samples, matte_areas, fit_note) -> (ok, reason)`. A take fails when:
      - faces are found in under half the samples, or
      - the fit wanted a scale outside [0.8, 1.25], or
      - more than 5% of frames have a matte area more than 25% off the take's median.
    - `on_failure(recorded_theme, output_theme) -> "fallback" | "keep"`
    - `over_budget(elapsed_s, frames_done, frames_total, budget_s=1800) -> bool`
    - `chunks(n_frames, workers) -> [(first, count)]`: covers `[0, n)` exactly, with no overlap.

**Algorithm per take:**
1. Skip a take whose `kind` isn't `"source"`.
2. Measure:
   - Pick six sample frames, with `fixups`-style frame-index reads.
   - Get Vision mattes (`keep` = 2 on M, 1 on L/R).
   - Run `face_boxes`.
   - Compute the recorded theme from the middle sample.
   - Compute the alpha.
   - Find the edge touches.
   - Run `fit` with the output theme's geometry.
3. Gather old-graphic boxes. For a program recording, take the analysis `baked_overlays` (`{zone, first, last}`) that overlap the take. Each one's box is `verify_render.ZONES[zone]`, and `cut_boxes` adds its 30 px pad. They apply on frames `first..last` of the recording.
4. Run `share_transforms` across all takes.
5. Render: split into `chunks`, and give each chunk to a `multiprocessing` worker. Each worker:
   - runs its own `anchor_matte` and ffmpeg reader from `first - 1` to `first + count + 1`, so `steady` has neighbours
   - composites as the prototype's render loop does, with `cut_boxes` applied on old-graphic frames
   - writes a chunk file
6. Concat the chunks with the ffmpeg concat demuxer (`-c copy`).
7. Check the time guard after the first chunk of the first take finishes: `over_budget` projects over all takes.

- [ ] **Step 1: Write failing tests** for the pure functions:
  - `test_chunks_cover_every_frame_once`: `chunks(1000, 7)`. The counts sum to 1000, the firsts are increasing, and there are no gaps.
  - `test_failed_take_falls_back_only_when_the_set_stays_the_same`: `on_failure("", "") == "fallback"`, `on_failure("", "halloween") == "keep"`, `on_failure("halloween", "") == "keep"`.
  - `test_take_with_no_faces_fails`, `test_take_with_a_jumping_matte_fails`, `test_steady_take_passes`.
  - `test_budget_projection`: `over_budget(100, 1000, 30000)` is True (3000 s projected); `over_budget(10, 1000, 30000)` is False.
- [ ] **Step 2:** Run `pytest tests/test_recomposite_plan.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement the pure functions, then the CLI. Whole-show fallback writes `"abandoned": "<sentence>"` and makes no clips.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5: Fixture run (manual).**
  - Make a job-like `showDir` for `~/InFocusEditor/fixtures/2026-10-21/` from its existing `golden/` EDL. Copy it, add `"recomposite": true`, and run `render_graphics.mjs`.
  - Run `time uv run … recomposite.py <showDir>`.
  - Record wall time ÷ the total take seconds. Expected: ≤ 2.5.
  - View every `-after.jpg` next to the reference stills (`~/Downloads/InFocus News _ Thursday, November 13th, 2025 {0-45,1-36,1-57} screenshot.png`).
- [ ] **Step 6: Speed gate.**
  - If the ratio is > 2.5, profile with `cProfile` on one chunk and fix the top cost: cache `texture(plate)` and `table`, use float32 throughout, and replace `np.median` on 3 frames with `np.maximum(np.minimum(a,b), np.minimum(np.maximum(a,b), c))`.
  - Re-measure.
  - If it is still > 2.5, stop and report to the user with the numbers before porting anything to Core Image.
- [ ] **Step 7:** `git commit -m "feat(skill): recomposite takes onto the virtual set"`

---

### Task 6: Build reads re-composited takes

**Files:**
- Modify: `.claude/skills/infocus-show/scripts/build_ffmpeg.py` (`plan`, `filtergraph`, `layout_json`, `fcpxml`)
- Test: `tests/test_build_ffmpeg.py`

**Interfaces:**
- Consumes: `common.recomposite_on`, `common.recomposite_file`, `Recomposite/plan.json`.
- Produces:
  - A source segment whose plan entry has `status == "recomposited"` and whose clip exists is read from that clip, starting at frame 0, with no seek offset and no card or foreground layers.
  - `layout.json` segments carry `"recomposite": true` for those segments.
  - `fcpxml` references the clip as that segment's asset.
  - When `recomposite_on(edl)` is true but `plan.json` is missing, or `"abandoned"` is set, build as today.

- [ ] **Step 1: Write failing tests** in the existing `built` fixture style. Add a second fixture, `built_recomposited`:
  - Setup: same EDL plus `"recomposite": true`, a `Recomposite/plan.json` marking take `a` as recomposited, and `Recomposite/a - Recomposite.mov` = 150 frames of solid magenta.
  - `test_recomposited_take_reads_its_clip`: render frames 0–149 are magenta at the centre.
  - `test_layout_marks_recomposited_takes`.
  - `test_abandoned_plan_builds_as_today`: frame-number checks identical to `test_source_segments_are_cut_by_frame_index`.
- [ ] **Step 2:** Run `uv run --with numpy --with pillow --with soundfile --with pytest pytest tests/test_build_ffmpeg.py -v`. Expected: the new tests fail, the old ones pass.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: all PASS, including the `fidelity` ones if their fixtures are present.
- [ ] **Step 5:** `git commit -m "feat(skill): build reads recomposited takes"`

---

### Task 7: Verify the mixed-set rule and themed studio frames

**Files:**
- Modify: `verify_render.py` (add `RULES[12] = "set"` and `rule_set(show_dir, edl)`, and call it in `verify`)
- Test: `tests/test_recomposite_verify.py`

**Interfaces:**
- `rule_set(show_dir, edl) -> [failure]`. When `recomposite_on(edl)` and `plan.json` exist, it fails rule 12 for:
  - a take with `status == "fallback"` whose `recorded_theme` ≠ `plan["theme"]`, or
  - a plan `theme` ≠ `common.theme_name(edl)`.
- The message names the take, e.g. `"take 'chess_club' is on the regular set but the show is on the halloween set"`.

- [ ] **Step 1: Write failing tests:**
  - `test_mixed_sets_fail_rule_12`.
  - `test_same_set_fallback_passes`.
  - `test_recomposited_side_take_still_reads_as_its_set`:
    - Setup: a synthetic Halloween Left set PNG, with a card-dark rectangle at `SCREENS["L"]` and a person block.
    - Expected: `analyze.classify` at analysis resolution returns `"L"`. If it doesn't, fix `classify` or `middle_set` to load every installed theme's side sets (the same pattern as `middle_set`), and keep `tests/test_skill_studio_check.py` passing.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `pytest tests/test_recomposite_verify.py tests/test_skill_studio_check.py tests/test_verify_render.py -v`. Expected: PASS.
- [ ] **Step 5:** `git commit -m "feat(skill): verify rule 12, no mixed sets"`

---

### Task 8: Poster from the dual third

**Files:**
- Create: `.claude/skills/infocus-show/scripts/poster_pick.py`
- Test: `tests/test_poster_pick.py`

**Interfaces:**
- CLI: `uv run --with numpy --with pillow python poster_pick.py <showDir> <render> <out_dir>`. It writes:
  - `<out_dir>/candidates.jpg`: up to 6 frames, each labelled with its render time in seconds.
  - `<out_dir>/candidates.json`: `[{"seconds", "score", "faces"}]`, best first.
- Pure functions:
  - `full_window(boxes) -> (first, last) | None`: the frames of an overlay clip whose alpha-box area is ≥ 98% of the maximum. Use `verify_render.alpha_boxes`.
  - `frame_score(faces) -> float`: the minimum over faces of `min(eyes / 0.25, 1) - 2 * max(mouth - 0.04, 0) - abs(yaw)`. A frame with fewer faces than expected scores `-inf`.
  - `candidates(scores, k=6, min_gap=30) -> [frame]`: the top k frames, at least `min_gap` apart.

**Where the window comes from:**
- The dual third is the EDL overlay with `render.type == "dual"`. Its render frames come from `layout.json` `overlays[id].rec_in`, mapped through the overlay clip's rate.
- With no dual third: the first M take's first 4 s, after skipping its first 1 s.
- Expected faces: 2.
- Sample every 3rd frame. Grab frames with ffmpeg's `select` by index (never `-ss`), and run `face_marks` on them.

- [ ] **Step 1: Write failing tests:**
  - `test_full_window_skips_the_animation`: the boxes grow over 10 frames, hold for 50, then shrink over 10. Expected: `(10, 59)`.
  - `test_blink_and_open_mouth_score_lower`: eyes 0.3 / mouth 0.02 beats eyes 0.05, and beats mouth 0.12.
  - `test_missing_face_is_never_picked`.
  - `test_candidates_are_spread_out`.
- [ ] **Step 2:** Run `pytest tests/test_poster_pick.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the tests. Expected: PASS. Then run it on the fixture render from Task 10 and view `candidates.jpg`.
- [ ] **Step 5:** `git commit -m "feat(skill): pick the poster from the dual third by face quality"`

---

### Task 9: Brief field, skill text and docs

**Files:**
- Modify: `editor/jobfiles.py` (`"recomposite": bool(b.get("recomposite"))` next to `"note"`), `docs/job-contract.md`, `docs/portal-api.md`, `SKILL.md`, `references/edl.md`, `references/gotchas.md`, `tests/test_skill_contract.py`, `tests/test_runner.py`

**SKILL.md changes:**
- **Inputs list:** `recomposite`: the producer ticked "Re-set the anchors on the virtual set".
- **Step 4, a new rule:** recomposite is on when `brief.recomposite`, `brief.note` asks for it, or the show is themed. Record which one as `other`. Set `"recomposite": true` at the top of `edl.json`.
- **Step 6:** after `render_graphics.mjs`, when recomposite is on, run `recomposite.py <showDir>`, and `foreground.py --only <id>` only for fallback takes with a card.
  - View one `-after.jpg` per camera.
  - Copy every plan `flag` into `decisions.json` as a flagged `other`.
  - An `abandoned` plan is one flagged `other`.
  - Tight two-shots get the note's `other` (not flagged).
- **"Graphics go over the picture as they are":** reword the anchors bullet. The anchors stay as recorded unless the show is re-composited, and then only `recomposite.py` moves them, one transform per take. Never reframe, rescale or recomposite the anchors by any other means, never to get past a check, and never swap in the clean set image yourself.
- **Step 8:** add rule 12 (mixed sets) to the list. Keep "reframe, rescale or recomposite the anchors" in the never-list.
- **Step 9 poster:** run `poster_pick.py`, view `candidates.jpg`, and pick the frame with both anchors looking natural (eyes open, mouth at rest, facing camera). Then run `poster.py` at its seconds. Record the pick as `other` when it isn't the top score.
- **`gotchas.md`:** the same rewording at line 60, plus a "Recomposite" bullet group:
  - the plate method needs the recorded set
  - two-shots cut at the bottom sit low
  - green screen isn't supported yet
- **`edl.md`:** document the top-level `"recomposite"`.

**Tests:**
- [ ] **Step 1: Update and write tests:**
  - `test_skill_keeps_graphics_and_anchors_as_they_are` keeps its existing needles and adds `"only \`recomposite.py\` moves them"`.
  - New `test_skill_names_the_recomposite_triggers`: SKILL.md contains `brief.recomposite`, `recomposite.py`, `poster_pick.py` and `rule 12`.
  - `tests/test_runner.py`: a new `test_recomposite_reaches_the_brief`. A claim brief with `"recomposite": True` gives `brief.json["recomposite"] is True`, and a missing value gives `False`.
- [ ] **Step 2:** Run `pytest tests/test_skill_contract.py tests/test_runner.py -v`. Expected: the new assertions FAIL.
- [ ] **Step 3:** Make the edits.
- [ ] **Step 4:** Run the full suite with `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5:** `git commit -m "feat: recomposite opt-in in the brief, skill and docs"`

---

### Task 10: End-to-end on the fixture show

- [ ] **Step 1:** In a scratch copy of `~/InFocusEditor/fixtures/2026-10-21/` with its golden `edl.json`:
  - Set `"recomposite": true`.
  - Run `render_graphics.mjs` → `recomposite.py` → `master_audio.py` → `build_ffmpeg.py` → `check_render.py` → `verify_render.py` → `poster_pick.py`.
  - Expected: `verify.json` `"pass": true`.
- [ ] **Step 2:** Repeat with `"theme": "halloween"` and the Halloween package files installed under `Themes/Halloween/` in a scratch copy of the package. Expected: a pass, and every after-still on the Halloween set.
- [ ] **Step 3:** Send the user one contact sheet (`check/segments.jpg`) for each run plus the chosen poster with SendUserFile, and ask whether the seating matches the references. Change seat constants only on their feedback.
- [ ] **Step 4:** Ask for a review with `mcp__consult__review_changes`, then use superpowers:finishing-a-development-branch.

---

### Task 11 (infocus-packages): the Portal checkbox

**Files** (repo `~/Documents/GitHub/infocus-packages`, branch `feat/recomposite-checkbox`):
- Modify: `prisma/schema.prisma` (`ShowEditJob.recomposite Boolean @default(false)`), plus a new migration via `npx prisma migrate dev --name show_edit_recomposite`.
- Modify: `app/api/show-roles/edits/route.ts` (`recomposite: z.boolean().optional()` on `start` and `redo`), and `src/server/show-editor.ts`:
  - `startShowEdit` and `redoShowEdit` store the flag. Redo defaults to the old job's value when it's omitted.
  - `showEditView` exposes it.
  - The claim brief (the `note: job.note` at line ~424) adds `recomposite: job.recomposite`.
- Modify: `app/(app)/editing/use-show-edit.ts` (action types) and `app/(app)/editing/editing-show-view.tsx`:
  - A checkbox in the Start instructions area and in `RedoControl`, labelled **"Re-set the anchors on the virtual set"**, with the helper line "Cuts the anchors out and seats them properly on the set. Slower edit."
  - `onStart` and `onRedo` pass it.
- Test: `tests/show-editor.test.ts`, `tests/show-edits-route.test.ts`.

- [ ] **Step 1: Write failing tests:**
  - Start with `recomposite: true` stores it, and the claim brief carries `recomposite: true`.
  - Redo without the field keeps the old job's value.
  - The route rejects `recomposite: "yes"` with a 400.
- [ ] **Step 2:** Run `npx vitest run tests/show-editor.test.ts tests/show-edits-route.test.ts`. Expected: FAIL.
- [ ] **Step 3:** Implement, following the existing `note` handling exactly.
- [ ] **Step 4:** Run the same command, then `npx tsc --noEmit`. Expected: PASS.
- [ ] **Step 5:** Commit on the branch. Ask the user before pushing or opening a PR, and don't run the migration against production.
