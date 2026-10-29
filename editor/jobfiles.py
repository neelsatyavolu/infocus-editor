"""The job folder contract (docs/job-contract.md): paths, brief.json,
status.json and result.json."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from editor.config import Config
from editor.portal import Claim


@dataclass(frozen=True)
class JobPaths:
    root: Path

    @property
    def brief(self) -> Path:
        return self.root / "brief.json"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    @property
    def result(self) -> Path:
        return self.root / "result.json"

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def packages(self) -> Path:
        return self.root / "packages"

    @property
    def house_rules(self) -> Path:
        return self.root / "house-rules.md"

    @property
    def show(self) -> Path:
        return self.root / "show"

    @property
    def out(self) -> Path:
        return self.root / "out"

    def output(self, show_date: str, version: int) -> Path:
        return self.out / f"InFocus Show {show_date} v{version}.mp4"

    def poster(self, show_date: str, version: int) -> Path:
        return self.out / f"InFocus Show {show_date} v{version}.poster.jpg"


def job_paths(cfg: Config, claim: Claim) -> JobPaths:
    return JobPaths(cfg.work_root / f"{claim.show_date}-{claim.job_id}")


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w.\- ]+", "_", name).strip(" .")
    return cleaned or "file"


def package_local_name(index: int, nas_path: str) -> str:
    return f"{index + 1:02d}-{safe_name(Path(nas_path).name)}"


def build_brief(cfg: Config, claim: Claim, paths: JobPaths, recordings: Path, source: Path | None = None
                ) -> dict[str, Any]:
    """brief.json for the skill. `recordings` is show/recordings.json (what the files are);
    `source` is set only when the job is exactly one program file, for older skills."""
    b = claim.brief
    packages = [
        {"title": p.get("title") or "", "localPath": str(paths.packages / package_local_name(i, p["nasPath"])),
         "toss": p.get("toss")}
        for i, p in enumerate(b.get("packages") or [])
    ]
    fix = {"text": claim.fix.text, "remember": claim.fix.remember} if claim.fix else None
    return {
        "jobId": claim.job_id,
        "kind": claim.kind,
        "showDate": claim.show_date,
        "filmedOn": b.get("filmedOn"),
        "version": claim.version,
        "recordings": str(recordings),
        "source": str(source) if source else None,
        "anchors": list(b.get("anchors") or []),
        "announcements": list(b.get("announcements") or []),
        "submissions": list(b.get("submissions") or []),
        "pastCards": list(b.get("pastCards") or []),
        "roster": list(b.get("roster") or []),
        "packages": packages,
        "note": b.get("note"),
        "recomposite": bool(b.get("recomposite")),
        "fix": fix,
        "showDir": str(paths.show),
        "output": str(paths.output(claim.show_date, claim.version)),
        "poster": str(paths.poster(claim.show_date, claim.version)),
        "packageDir": str(cfg.package_dir),
        "houseRules": str(paths.house_rules),
    }


MAX_HOUSE_RULES = 20_000


def write_house_rules(cfg: Config, claim: Claim, paths: JobPaths) -> Path:
    """The Portal's house rules (brief.houseRules), or the built-in example when it has none,
    written into the job folder for the agent to read (and, on a remembered fix, edit)."""
    text = claim.brief.get("houseRules")
    if not isinstance(text, str) or not text.strip():
        text = cfg.house_rules_example.read_text()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.house_rules.write_text(text[:MAX_HOUSE_RULES])
    return paths.house_rules


def write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def edl_cards(edl_path: Path, announcements: list[str]) -> list[dict[str, Any]]:
    """The announcement cards of a finished edit, for the Portal to offer later edits as examples.
    A card's `script` is the index of the brief announcement it was written from, if the skill set one."""
    edl = read_json(edl_path) or {}
    cards = []
    for seg in edl.get("segments") or []:
        card = seg.get("card") if isinstance(seg, dict) else None
        if not isinstance(card, dict) or not str(card.get("title") or "").strip():
            continue
        index = card.get("script")
        script = announcements[index] if isinstance(index, int) and 0 <= index < len(announcements) else None
        cards.append({"title": str(card["title"]).replace("|", " ")[:120],
                      "points": [str(p)[:160] for p in card.get("points") or []][:6],
                      "script": script[:1500] if script else None})
    return cards


def edl_tosses(edl_path: Path, packages: list[dict[str, Any]]) -> list[str | None]:
    """What the anchors said at each package's throw (a package segment's `toss_said`), in brief package order,
    for the YouTube description when a package has no written toss."""
    said = {}
    for seg in (read_json(edl_path) or {}).get("segments") or []:
        if isinstance(seg, dict) and seg.get("kind") == "file" and str(seg.get("toss_said") or "").strip():
            said[str(seg.get("path"))] = str(seg["toss_said"]).strip()[:1500]
    return [said.get(p.get("localPath")) for p in packages]


def _fps(value: Any) -> float:
    try:
        num, _, den = str(value).partition("/")
        return float(num) / float(den or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def edl_chapters(edl_path: Path, layout_path: Path, packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The finished show's chapters, `[{title, start}]` (seconds into the render), for the Portal's player and the
    YouTube description. A segment's `chapter` (set by the skill) names a new chapter; otherwise intro and outro,
    each announcement (its card title), each package and the anchors' open and sign-off get one, and anything else
    continues the chapter before it."""
    edl, layout = read_json(edl_path) or {}, read_json(layout_path) or {}
    fps = _fps(layout.get("fps") or edl.get("fps"))
    starts = {s.get("id"): s.get("rec_in") for s in layout.get("segments") or [] if isinstance(s, dict)}
    segs = [s for s in edl.get("segments") or [] if isinstance(s, dict) and isinstance(starts.get(s.get("id")), int)]
    if fps <= 0 or not segs:
        return []
    titles = {p.get("localPath"): p.get("title") for p in packages}
    later_story = [any(x.get("card") or x.get("kind") in ("file", "placeholder") and "outro" not in str(x.get("id", "")).lower()
                       for x in segs[i + 1:]) for i in range(len(segs))]
    chapters: list[dict[str, Any]] = []
    for i, seg in enumerate(segs):
        name, kind = str(seg.get("id", "")).lower(), seg.get("kind")
        title = str(seg.get("chapter") or "").strip()
        if not title:
            if kind == "file" and "intro" in name:
                title = "Intro"
            elif kind == "file" and "outro" in name:
                title = "Outro"
            elif kind in ("file", "placeholder"):
                pkg = titles.get(seg.get("path"))
                title = f"Package: {pkg}" if pkg else "Package"
            elif isinstance(seg.get("card"), dict) and seg["card"].get("title"):
                title = str(seg["card"]["title"]).replace("|", " ").strip()
            elif not chapters or chapters[-1]["title"] == "Intro":
                title = "Welcome"
            elif not later_story[i]:
                title = "Sign-off"
        if title and (not chapters or chapters[-1]["title"] != title):
            chapters.append({"title": title[:100], "start": round(starts[seg["id"]] / fps, 2)})
    return chapters


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
