"""Shared fakes for the worker tests."""
from __future__ import annotations

from pathlib import Path

from editor.config import REPO_ROOT, Config


def make_config(tmp_path: Path, **overrides) -> Config:
    data = tmp_path / "data"
    values = dict(
        data_dir=data,
        portal_url="https://portal.example.org",
        package_dir=tmp_path / "package",
        work_root=data / "jobs",
        agents=("claude", "codex", "grok"),
        max_turns=300,
        max_budget_usd=None,
        run_timeout_minutes=75,
        keep_days=14,
        version="0.1.0",
        repo_root=REPO_ROOT,
        update_repo="example/infocus-editor",
        auto_update=False,
        require_signed_tags=False,
        editor_token="editor-test-token",
    )
    values.update(overrides)
    return Config(**values)
