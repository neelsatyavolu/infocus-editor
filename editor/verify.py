"""Run the skill's verify_render.py (docs/job-contract.md) and read its verdict."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

VERIFY_DEPS = ("numpy", "opencv-python", "pillow", "soundfile")
TIMEOUT_S = 20 * 60

Run = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Verdict:
    passed: bool
    failures: tuple[dict[str, Any], ...] = ()
    loudness: tuple[dict[str, Any], ...] = field(default=())


def verify_command(skill_dir: Path, show_dir: Path, render: Path) -> list[str]:
    cmd = ["uv", "run"]
    for dep in VERIFY_DEPS:
        cmd += ["--with", dep]
    return cmd + ["python", str(skill_dir / "scripts" / "verify_render.py"), str(show_dir), "--render", str(render)]


def run_verify(skill_dir: Path, show_dir: Path, render: Path, run: Run = subprocess.run) -> Verdict:
    if not render.is_file():
        return Verdict(False, ({"rule": "render", "detail": f"No render at {render.name}"},))
    try:
        proc = run(verify_command(skill_dir, show_dir, render), capture_output=True, text=True,
                   check=False, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return Verdict(False, ({"rule": "verify", "detail": "verify_render.py timed out"},))
    report_path = show_dir / "check" / "verify.json"
    try:
        report = json.loads(report_path.read_text())
    except (OSError, ValueError):
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        return Verdict(False, ({"rule": "verify", "detail": f"verify_render.py exited {proc.returncode}: {tail}"},))
    passed = bool(report.get("pass")) and proc.returncode == 0
    failures = tuple(report.get("failures") or ())
    if not passed and not failures:
        failures = ({"rule": "verify", "detail": f"verify_render.py exited {proc.returncode}"},)
    return Verdict(passed, failures, tuple(report.get("loudness") or ()))
