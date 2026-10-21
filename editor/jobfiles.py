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


def build_brief(cfg: Config, claim: Claim, paths: JobPaths, source: Path) -> dict[str, Any]:
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
        "source": str(source),
        "anchors": list(b.get("anchors") or []),
        "announcements": list(b.get("announcements") or []),
        "packages": packages,
        "note": b.get("note"),
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


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_brief_source(paths: JobPaths) -> Path | None:
    brief = read_json(paths.brief)
    return Path(brief["source"]) if brief and brief.get("source") else None
