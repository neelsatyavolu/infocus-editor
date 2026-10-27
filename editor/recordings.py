"""Work out what a show's files are from their content (docs/multicam.md, "Detection").

TriCaster names and times carry nothing reliable, so:
1. filter: only 1080p video; byte-identical duplicates and Ignore'd files are skipped
2. fingerprint: duration, a 1 kHz audio envelope, and (when needed) a picture profile
3. group: files whose envelopes correlate strongly were recorded together (one recording)
4. roles: one file is a program recording; in a group, the program feed is the one with
   several cameras, full-screen cards or frequent cuts, and every other file is the camera
   of its majority class. A producer's role from the Portal overrides detection.
5. order recordings by export time, write show/recordings.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from editor.media import HOUSE_FPS, MediaInfo, conform, probe
from editor.sources import fingerprint
from editor.sync import MIN_CONFIDENCE, SyncResult, audio_envelope, correlate_envelopes

FPS = float(HOUSE_FPS)
FPS_TEXT = f"{HOUSE_FPS.numerator}/{HOUSE_FPS.denominator}"
PROFILE_EVERY_S = 2.0
CAMERAS = ("M", "L", "R")
AMBIGUOUS_RATIO = 1.5  # between this and MIN_CONFIDENCE: maybe the same recording
SOLID_RATIO = 3.0  # grouped below this: grouped, but a person should check
PROGRAM_FS_SHARE = 0.03
PROGRAM_CUTS_PER_MIN = 2.0
CAMERA_SHARE = 0.15  # a camera class counts as present in a feed above this share
CLEAR_MAJORITY = 0.6
CONTRADICTED = 0.05  # a Portal camera role the feed shows on under this share, against a clear majority, is a mix-up


class DetectionError(Exception):
    """None of the files can be edited; the message says why."""


@dataclass(frozen=True)
class Candidate:
    local: Path
    name: str  # the file's name on the NAS
    role: str = "auto"  # auto | program | M | L | R | ignore (from the Portal)
    order: int = 0
    mtime: float | None = None  # export finish time, when the Portal sends it


@dataclass(frozen=True)
class Profile:
    classes: tuple[str, ...]
    every_s: float = PROFILE_EVERY_S

    def share(self, cls: str) -> float:
        return self.classes.count(cls) / len(self.classes) if self.classes else 0.0

    @property
    def cuts_per_min(self) -> float:
        cuts = sum(1 for a, b in zip(self.classes, self.classes[1:]) if a != b)
        minutes = len(self.classes) * self.every_s / 60
        return cuts / minutes if minutes else 0.0

    @property
    def cameras_present(self) -> list[str]:
        return [c for c in CAMERAS if self.share(c) >= CAMERA_SHARE]

    def looks_program(self) -> bool:
        return (len(self.cameras_present) >= 2 or self.share("FS") >= PROGRAM_FS_SHARE
                or self.cuts_per_min >= PROGRAM_CUTS_PER_MIN)

    def camera_ranking(self) -> list[tuple[str, float]]:
        """Camera classes by their share of the frames that show a camera at all."""
        counts = Counter(c for c in self.classes if c in CAMERAS)
        total = sum(counts.values())
        return sorted(((c, counts[c] / total) for c in CAMERAS if counts[c]), key=lambda x: -x[1]) if total else []


@dataclass(frozen=True)
class Detection:
    path: Path
    data: dict[str, Any]
    source: Path | None  # the one program file, when the job is exactly that (older skills read it)
    decisions: tuple[dict[str, Any], ...] = field(default=())
    needs_review: bool = False

    @property
    def total_s(self) -> float:
        return sum(r["frames"] for r in self.data["recordings"]) / FPS


@dataclass
class _File:
    cand: Candidate
    info: MediaInfo
    env: np.ndarray | None = None
    role: str = ""
    offset_s: float = 0.0
    path: Path | None = None


def _decision(kind: str, detail: str, flagged: bool) -> dict[str, Any]:
    return {"kind": kind, "detail": detail[:300], "flagged": flagged}


def _rel(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


def profile_with_skill(skill_scripts: Path, package_dir: Path, run=subprocess.run) -> Callable[[Path], Profile]:
    """The real picture profile: the skill's classifier on one frame every 2 s (editor/frame_profile.py)."""
    helper = Path(__file__).with_name("frame_profile.py")

    def profile(path: Path) -> Profile:
        cmd = ["uv", "run", "--quiet", "--with", "numpy", "--with", "opencv-python", "--with", "pillow",
               "--with", "soundfile", "python", str(helper), str(skill_scripts), str(package_dir), str(path),
               str(PROFILE_EVERY_S)]
        proc = run(cmd, capture_output=True, text=True, check=False, timeout=1800,
                   env={**os.environ, "INFOCUS_PACKAGE": str(package_dir)})
        try:
            classes = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            print(f"frame profile failed for {path.name}: {proc.stderr[-300:]}", file=sys.stderr)
            classes = []
        return Profile(tuple(classes))

    return profile


class Detector:
    def __init__(self, show_dir: Path, *, run=subprocess.run,
                 envelope: Callable[[Path], np.ndarray | None] | None = None,
                 profile: Callable[[Path], Profile] | None = None,
                 conform_to: Callable[[Path, Path], Path] | None = None):
        self._show = show_dir
        self._run = run
        self._envelope = envelope or (lambda p: audio_envelope(p, run))
        self._profile = profile or (lambda p: Profile(()))
        self._conform = conform_to or (lambda src, dest: conform(src, dest, run))
        self._decisions: list[dict[str, Any]] = []
        self._skipped: list[dict[str, str]] = []
        self._review = False

    # ---- 1. filter ----------------------------------------------------------------

    def _skip(self, cand: Candidate, reason: str, flagged: bool) -> None:
        self._skipped.append({"path": str(cand.local), "reason": reason})
        self._decisions.append(_decision("skip", f"Skipped {cand.name}: {reason}", flagged))
        self._review = self._review or flagged

    def _filter(self, candidates: Sequence[Candidate]) -> list[_File]:
        kept: list[_File] = []
        seen: dict[tuple[int, str], Candidate] = {}
        for cand in sorted(candidates, key=lambda c: c.order):
            if cand.role == "ignore":
                self._skip(cand, "set to Ignore in the Portal", False)
                continue
            info = probe(cand.local, self._run)
            if not info.has_video:
                self._skip(cand, "it has no video", True)
                continue
            if (info.width, info.height) != (1920, 1080):
                sd = info.height <= 576
                self._skip(cand, f"{'SD capture' if sd else 'not 1080p'} ({info.width}x{info.height})", True)
                continue
            key = fingerprint(cand.local)
            if key in seen:
                self._skip(cand, f"duplicate of {_rel(seen[key].local)} ({seen[key].name})", False)
                continue
            seen[key] = cand
            kept.append(_File(cand, info))
        return kept

    # ---- 3. group --------------------------------------------------------------------

    def _group(self, files: list[_File]) -> list[tuple[list[int], dict[tuple[int, int], SyncResult]]]:
        for f in files:
            f.env = self._envelope(f.cand.local) if f.info.has_audio else None
        parent = list(range(len(files)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        strong: dict[tuple[int, int], SyncResult] = {}
        maybe: list[tuple[int, int, SyncResult]] = []
        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                a, b = files[i].env, files[j].env
                if a is None or b is None:
                    continue
                result = correlate_envelopes(a, b)
                d = result.offset_s
                overlap = min(files[i].info.duration, files[j].info.duration - d) - max(0.0, -d)
                if result.confidence >= MIN_CONFIDENCE and overlap > 0:
                    strong[(i, j)] = result
                    parent[find(j)] = find(i)
                elif result.confidence >= AMBIGUOUS_RATIO:
                    maybe.append((i, j, result))
        groups: dict[int, list[int]] = {}
        for i in range(len(files)):
            groups.setdefault(find(i), []).append(i)
        for i, j, result in maybe:
            if find(i) != find(j):
                self._review = True
                self._decisions.append(_decision(
                    "grouping", f"{files[i].cand.name} and {files[j].cand.name} might be the same recording "
                                f"(weak audio match, ratio {result.confidence:.1f}); treated as separate", True))
        return [(members, {k: v for k, v in strong.items() if k[0] in members}) for members in groups.values()]

    # ---- offsets and roles ----------------------------------------------------------

    def _offsets(self, files: list[_File], members: list[int], edges: dict[tuple[int, int], SyncResult]) -> float:
        """Offsets relative to the first member (file time = root time + offset); returns the weakest ratio."""
        root = members[0]
        files[root].offset_s = 0.0
        done, queue, weakest = {root}, [root], float("inf")
        while queue:
            i = queue.pop(0)
            for (a, b), result in sorted(edges.items(), key=lambda kv: -kv[1].confidence):
                if a == i and b not in done:
                    files[b].offset_s = files[a].offset_s + result.offset_s
                elif b == i and a not in done:
                    files[a].offset_s = files[b].offset_s - result.offset_s
                else:
                    continue
                nxt = b if a == i else a
                done.add(nxt)
                queue.append(nxt)
                weakest = min(weakest, result.confidence)
        return weakest

    def _roles(self, files: list[_File], members: list[int]) -> None:
        if len(members) == 1:
            f = files[members[0]]
            f.role = f.cand.role if f.cand.role in ("program", *CAMERAS) else "program"
            self._check_portal_cameras([f] if f.role in CAMERAS else [])
            return
        auto: list[tuple[_File, Profile]] = []
        for i in members:
            f = files[i]
            if f.cand.role in ("program", *CAMERAS):
                f.role = f.cand.role
            else:
                auto.append((f, self._profile(f.cand.local)))
        self._check_portal_cameras([files[i] for i in members if files[i].cand.role in CAMERAS])
        taken = {files[i].role for i in members if files[i].role in CAMERAS}
        cameras: list[tuple[_File, Profile]] = []
        for f, prof in auto:
            if prof.looks_program():
                f.role = "program"
            else:
                cameras.append((f, prof))
        # Strongest claims first, so a clash goes to the file that shows that camera most.
        claims = sorted(((f, prof, cls, share) for f, prof in cameras for cls, share in prof.camera_ranking()),
                        key=lambda x: -x[3])
        for f, prof, cls, share in claims:
            if f.role or cls in taken:
                continue
            f.role, taken = cls, taken | {cls}
            ranking = prof.camera_ranking()
            if ranking and ranking[0][0] != cls:
                self._flag("roles", f"{f.cand.name} looked most like camera {ranking[0][0]}, which another file "
                                    f"showed more; used it as {cls} ({share:.0%} of its frames)")
            elif share < CLEAR_MAJORITY:
                self._flag("roles", f"{f.cand.name}'s camera wasn't clear; used it as {cls} ({share:.0%} of its frames)")
        for f, prof in cameras:
            if not f.role:
                f.role = "program"
                self._flag("roles", f"Couldn't tell which camera {f.cand.name} shows; used it as a program feed")

    def _check_portal_cameras(self, overridden: list[_File]) -> None:
        """A producer's camera role stands unless the feed's picture clearly shows another camera (e.g. L and R
        swapped in the Portal): then the picture wins, as long as no two files end up on one camera. Recorded,
        not flagged: verify rule 8 still fails any side take that isn't on its set."""
        proposed: dict[Path, tuple[_File, str, float]] = {}       # by local path: Portal names can repeat
        for f in overridden:
            prof = self._profile(f.cand.local)
            ranking = prof.camera_ranking()
            shares = dict(ranking)
            # the other camera must fill most of the whole feed, not just most of its few camera frames
            if ranking and ranking[0][0] != f.role and prof.share(ranking[0][0]) >= CLEAR_MAJORITY \
                    and shares.get(f.role, 0.0) < CONTRADICTED:
                proposed[f.cand.local] = (f, ranking[0][0], shares.get(f.role, 0.0))
        final = {f.cand.local: f.role for f in overridden} | {k: cam for k, (_, cam, _) in proposed.items()}
        if len(set(final.values())) < len(final):
            for f, cam, _ in proposed.values():
                self._flag("roles", f"{f.cand.name} was set to {f.role} in the Portal but shows camera {cam}; "
                                    "kept the Portal's role because the corrected roles would clash")
            return
        for f, cam, had in proposed.values():
            self._decisions.append(_decision("roles", f"{f.cand.name} was set to {f.role} in the Portal but shows "
                                                      f"camera {cam} ({had:.0%} {f.role}); used it as {cam}", False))
            f.role = cam

    def _flag(self, kind: str, detail: str) -> None:
        self._review = True
        self._decisions.append(_decision(kind, detail, True))

    # ---- the whole thing ------------------------------------------------------------

    def detect(self, candidates: Sequence[Candidate]) -> Detection:
        files = self._filter(candidates)
        if not files:
            reasons = [s["reason"] for s in self._skipped]
            if len(candidates) == 1 and reasons and reasons[0].startswith("SD capture"):
                raise DetectionError("This is the SD capture; pick the HD recording.")
            raise DetectionError("None of the files can be edited: " + "; ".join(reasons))
        recordings = []
        for members, edges in self._group(files):
            members.sort(key=lambda i: files[i].cand.order)
            weakest = self._offsets(files, members, edges)
            self._roles(files, members)
            recordings.append(self._recording(files, members, weakest))
        recordings.sort(key=lambda r: r.pop("_sort"))
        for n, rec in enumerate(recordings, 1):
            rec["id"] = f"r{n}"
            if rec.pop("_weak", False):
                self._flag("grouping", f"r{n}: the files only weakly match each other's audio; check they belong together")
        data = {"fps": FPS_TEXT, "recordings": [self._ordered(r) for r in recordings], "skipped": self._skipped}
        self._decisions.append(_decision("structure", structure_summary(data["recordings"]), False))
        self._show.mkdir(parents=True, exist_ok=True)
        out = self._show / "recordings.json"
        tmp = out.with_name(".recordings.json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, out)
        only = data["recordings"][0]["files"] if len(data["recordings"]) == 1 else []
        source = Path(only[0]["path"]) if len(only) == 1 and only[0]["role"] == "program" else None
        return Detection(out, data, source, tuple(self._decisions), self._review)

    def _recording(self, files: list[_File], members: list[int], weakest: float) -> dict[str, Any]:
        group = [files[i] for i in members]
        program_files = [f for f in group if f.role == "program"]
        for extra in program_files[1:]:  # the same recording exported twice: keep the first
            self._skip(extra.cand, f"another export of the same recording as {program_files[0].cand.name}", False)
            group.remove(extra)
        start = max(0.0, *(-f.offset_s for f in group))
        end = min(f.info.duration - f.offset_s for f in group)
        entries = []
        for f in group:
            path = f.cand.local
            if f.info.fps != HOUSE_FPS:
                path = self._conform(path, self._show / "conformed" / f"{path.stem}.mov")
                self._decisions.append(_decision("conform", f"Converted {f.cand.name} "
                                                 f"({float(f.info.fps or 0):.2f} fps) to 59.94", False))
            entries.append({"path": str(path), "role": f.role, "offsetFrames": round((start + f.offset_s) * FPS)})
        kind = "multicam" if any(e["role"] in CAMERAS for e in entries) else "program"
        ratio = weakest if len(group) > 1 else float("inf")
        confidence = 1.0 if ratio == float("inf") else round(1 - 1 / ratio, 3)
        times = [f.cand.mtime for f in group if f.cand.mtime is not None]
        sort_key = (min(times) if times else float("inf"), min(f.cand.order for f in group))
        return {"kind": kind, "frames": round((end - start) * FPS), "confidence": confidence, "files": entries,
                "_sort": sort_key, "_weak": len(group) > 1 and ratio < SOLID_RATIO}

    @staticmethod
    def _ordered(rec: dict[str, Any]) -> dict[str, Any]:
        order = {"program": 0, "M": 1, "L": 2, "R": 3}
        files = sorted(rec["files"], key=lambda f: order[f["role"]])
        return {"id": rec["id"], "kind": rec["kind"], "frames": rec["frames"], "confidence": rec["confidence"],
                "files": files}


def structure_summary(recordings: Sequence[dict[str, Any]]) -> str:
    parts = []
    for rec in recordings:
        roles = [f["role"] for f in rec["files"]]
        if rec["kind"] == "multicam":
            cams = ", ".join(r for r in roles if r in CAMERAS)
            parts.append(f"{rec['id']} multicam ({cams}{' + program' if 'program' in roles else ''})")
        else:
            parts.append(f"{rec['id']} program")
    n = len(recordings)
    return f"{n} recording{'s' if n != 1 else ''}: " + ", ".join(parts)
