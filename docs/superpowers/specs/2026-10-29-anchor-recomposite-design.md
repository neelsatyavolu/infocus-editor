# Anchors re-set on the virtual set, and a better poster

Two features for the `infocus-show` skill and the worker:
1. **Recomposite:** cut the anchors out of every shot and seat them properly on a clean virtual set. The picture comes out as a finished set, so any theme's set can be swapped in.
2. **Poster:** take the YouTube poster while the dual third is fully on screen, at the moment the anchors' faces look most natural.

## Why

- **Bad framing:** the studio cameras are often framed off. Anchors sit too big or too small, too high or too low, or over the TV card. A neighbour's shoulder or a chair gets into the side shots. The switcher keys the anchors onto the set wherever the camera put them.
- **Sets locked in at filming:** the set is baked into the recording, so a show filmed on the regular set can't go out on the Halloween or Thanksgiving set.
- **Raw feeds coming:** the studio will soon record the cameras without the virtual set baked in. Those feeds have to be composited onto a set by the edit, because nothing else will do it.
- **Poster:** the current poster is a fixed moment about 2 s into the open. It often catches someone mid-word or mid-blink.

**What good looks like:** the three reference stills from the 11-13-2025 show (`~/Downloads/InFocus News _ Thursday, November 13th, 2025 0-45 / 1-36 / 1-57 screenshot.png`), on last year's set:
- **L/R:** one anchor on their side of the frame, card on the TV beside them.
- **M:** both anchors centred in their halves.
- **Every shot:**
  - each anchor is the same size from shot to shot
  - the waist is at the table's back edge
  - forearms and clasped hands rest on the table top in front
  - the table's front panel is drawn over the body below that
  - no neighbour or studio junk in shot

## Phasing (decided by the user)

**No green-screen sample exists yet;** the user will supply one later. So:
- **This build:** baked-set recordings only.
- **Green screen:** the matte step is still built so a chroma-key path can slot in beside the baked-set path without reworking placement or compositing.
- **Later phase:** green-screen detection, the chroma key and the always-on rule, built and tested once the sample arrives.

## Goals

- Every take in the show (M, L and R; program and multicam recordings) can be re-composited: anchor matte → clean set → fresh card on the TV → anchors → table front panel → package overlays.
- Works on both kinds of source:
  - **Baked set:** today's recordings, with the set keyed in by the switcher.
  - **Raw green screen:** coming soon, no set in the picture.
- The output set comes from the show's theme: the regular 2026 set, or a theme's own Left/Middle/Right sets. Adding a set later should only take a `THEMES` entry and its files.
- The poster comes from the dual third's fully-on window and has the most natural faces in it.

## Non-goals

- Adding the Thanksgiving theme itself. The package exists in `~/Downloads/InFocus/Show Resources/InFocus Thanksgiving 2026 Package`, but wiring a new theme is its own small change. This design only makes sure a theme's sets get used. (My call.)
- Re-lighting or colour-matching the anchors to a set. Matte, scale and position only, as in the references.
- Moving the camera within a shot. Each shot gets one fixed scale and offset; the cameras are locked off.
- Changing package graphics: sizes, positions and timing stay as they are.

## Anti-goals (a failure even if it runs)

- **Visible matte damage:** hair chewed off, a halo or fringe of the old set or green around the anchor, missing fingers or hands, flicker at the edges.
- **Inconsistent size:** an anchor jumping in size or position between consecutive shots from the same camera.
- **Mixed sets:** a show that mixes sets, some shots re-composited onto the Halloween set and others still on the regular set.
- **Burned-in graphics:** an old name bar or card dragged along with the anchor into the new picture.
- **A broken show:** making the edit miss the 75-minute job limit, or making it fail where today's pipeline would have finished.

## When it runs (decided by the user)

**Opt-in** for baked-set recordings. It runs when any of these is true:
1. **Portal checkbox:** on the Start / Redo automatic editing dialog (infocus-packages, `app/(app)/editing/`). It travels as a new brief field, e.g. `"recomposite": true` (name left to the builder). Add it to `docs/portal-api.md` and `docs/job-contract.md`. The worker copies it into `brief.json` (`editor/jobfiles.py`, beside `note`).
2. **Producer's note:** `brief.note` asks for it ("put them on the set properly", "re-seat the anchors").
3. **Themed show:** the show's theme has its own sets (a themed show always goes out on that theme's sets).

**Always on** for raw green-screen feeds, which have no set otherwise.

**When it's off:** today's behaviour is unchanged: the recording's own picture, cards fitted on the TV, anchor foregrounds over the cards.

**Record it:** the skill records why it ran (checkbox, note, theme, green screen) as an `other` decision.

## How it works

### Starting point

The prototype on the local branch `experiment/anchor-reseat` (commit `9552c69`, `experiments/anchor-reseat/`: `reseat.py`, `anchormatte_video.swift`, `face_boxes.swift`, README). It was tested on the 10-09, 10-14, 10-16 and 10-21 shows. Build on it rather than starting over. The user settled its placement rule then: the waist on the table's back edge, the arms and hands on the table top in front, never hidden behind the table.

### 1. Which kind of source

Per recording file, decide whether it's a baked set or a green screen (a large share of key-green in the background).
- **Baked set:** also identify which set and theme it was filmed on. The analysis already recognises the regular and Halloween sets (`analyze.middle_set`, `classify`). The plate method needs the exact recorded set image.
- **Green-screen camera files:** the worker's role detection (`editor/frame_profile.py`, `editor/recordings.py`) has to recognise them, and tell M (two people) from L and R. Portal roles still override.
  - L vs R for a single-person feed: my guess is which side of the frame the person sits on. Left to the builder.

### 2. Matte (per frame)

- **Baked set:** the prototype's method:
  - Vision person instances, keeping the N largest people: 2 on M, 1 on L/R.
  - The difference from the recorded set plate only takes pixels away.
  - Gentler edge for hair above the chin; a 2 px choke below it.
  - A temporal median against shimmer.
  - Colour unmixing of edge pixels.
  - Inside the TV rectangle, the card's dark ink isn't counted as anchor.
- **Green screen:** a chroma key with spill suppression gives the edge. Vision instances still decide which people to keep (neighbours, crew), and the key decides edge quality.
- **Burned-in graphics:** in program recordings, the old overlay windows the analysis already found (name bars, dual thirds, Follow card) are excluded from the matte for their frames, so an old plate never travels with the anchor.

### 3. Placement (one transform per shot)

The prototype's `fit`:
- Faces are found on a few sampled frames.
- Scale from face height to the seat size.
- Across: faces onto the seats.
- Up and down: each waist, read from the elbows, onto the table's back edge.
- Keep 60 px of headroom, and never pull a frame-edge cut into view.

Constants (`SEATS`, `DESK_BACK`, `DESK_LIP`):
- **Default:** measured on the 2026 sets.
- **Per theme set:** if a theme's set has a different table, its constants live with that theme. The builder checks whether the Halloween and Thanksgiving tables match the 2026 one (the package README says their layouts and TV positions match).
- **Seats:** re-check against the reference stills, which show the target size and position: the prototype's seats came from the October shows.

Consistency: every shot from the same camera in one recording should land within a few px and about 2% scale of each other. Snap them to a shared transform when they're close, so the anchors don't creep between cuts.

**Matching the references (my call):** the reference stills are on last year's set, so match them in proportion to the 2026 table, not in pixels:
- Face size relative to the table width, waist on the table's back edge, hands on the top.
- Check one still per camera side by side with the references before going further.

**Known limit: two-shots cut off at the frame bottom (my call).** These can only rise about 30 px before the cut shows on the table top. Let them sit up to that limit, a little low, with the front panel hiding the cut. Never show the cut, and never fall back to the original picture for this. Record an `other` decision (not flagged) saying the camera was framed tight.

### 4. Composite

Layers, bottom to top:
1. The theme's set PNG (Left / Middle / Right).
2. The fresh card on the TV (L/R).
3. The anchors.
4. The table's front panel below `DESK_LIP`.
5. The package overlays, exactly as today.

The existing card and foreground layers in `build_ffmpeg.py` are replaced for re-composited takes. Whether the composite becomes a per-take intermediate clip or is streamed into the main ffmpeg run is the builder's call.

### 5. Fallback

A take fails when its matte or fit fails the sanity checks: no faces, an implausible scale, or the matte area jumping between frames. What happens then depends on the output set (decided by the user):
- **Output set = recorded set** (a regular show on the 2026 set): the take falls back to its original picture, which is the same set, so the switch is invisible. Flagged `other` decision.
- **Output set ≠ recorded set** (a themed show, or a show filmed on a theme's set going out on another): never fall back. Mixing sets is an anti-goal. The take uses its best matte and placement anyway, the decision is flagged, and the flag names the take so a producer checks that shot before it publishes.
- **Raw green screen** (later phase): same as the row above, since there's no original picture to fall back to.

The `verify_render.py` same-set check therefore fails the job on any mixed-set render.

### 6. Skill, checks and docs

- **SKILL.md:** replace "never reframe, rescale or recomposite the anchors / never swap in the clean set image" with "...unless the show is re-composited (see the new section)". The new section covers:
  - when it runs
  - what to view: one before/after/alpha still per camera
  - how to flag a fallback
- **Same edit in the other places:** `references/gotchas.md` and `tests/test_skill_contract.py`. The no-workaround rule stays: re-compositing is never used to get past a check.
- **`verify_render.py`:**
  - The studio check (rule 8, side set) and the card checks have to accept a re-composited take on the theme's set.
  - Rule 4's uncovered-graphic check must not fire on the clean set.
  - Add a check that every re-composited take uses the same theme set.
- **`timeline.fcpxml`:** still opens in Resolve, referencing the re-composited takes (or the intermediates).
- **Tests:** follow the repo's existing pytest style (`tests/test_skill_*.py`), with synthetic frames where possible.

### 7. Speed budget

- **Target:** the recompositing pass adds no more than about 20 minutes to an 8-minute show on the studio Mac. The job limit is 75 minutes for everything.
- **The prototype:** it does numpy work per frame at 1080p and is very likely far too slow for a whole show. Moving the per-frame work to Swift/Core Image (or Metal) is the obvious path; the builder decides.
- **Measure first:** time it on the 10-21 fixture (`~/InFocusEditor/fixtures/2026-10-21/`) before anything else.
- **Time guard (my call):** going over the 20 minutes is acceptable; the 75-minute job limit is the hard line. Before rendering, the pass estimates its own run time from the first takes. If the estimate would push the job past about 60 minutes:
  - **Baked-set, non-themed show:** the whole show falls back to the recorded picture (never part of it), with a flagged decision.
  - **Themed show or green screen:** it keeps going and the decision is flagged.

## Poster (independent of recomposite)

- **The window:** read `layout.json` for the dual third's on-screen window, from the end of its in-animation to the start of its out-animation.
- **Score:** sample frames across the window (every few frames) and score each anchor's face with Apple Vision landmarks: eyes open, mouth closed or neutral (not mid-word), face roughly toward camera. The existing `mouth_video.swift` landmark code is the starting point. The worst face in a frame sets that frame's score.
- **Agent picks:** put the top ~6 candidates on a contact sheet. The agent views it, picks the least awkward one, and runs `poster.py` at that time.
- **No dual third:** fall back to today's rule (an M two-shot in the open about 2 s in, no graphic animating), using the same scoring over that shot.
- **Update** step 9 of SKILL.md to match.

## Left to the builder

- **Matte module:** module boundaries, and whether the prototype's Python survives or moves into Swift.
- **Field name:** the brief field name and the Portal checkbox label.
- **Pipeline wiring:** intermediates vs. streaming, and how `layout.json` records re-composited takes.
- **Thresholds:** exact sanity thresholds for the fallback, and the green-screen detection threshold.
- **Poster:** the sampling rate and scoring weights.
