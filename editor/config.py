"""Settings from <data_dir>/config.toml plus the editor token from the macOS Keychain.

config.toml needs only `portal_url` (the installer writes it); everything else has a
default. The editor token comes from linking (`infocus-editor link`). AI sign-ins,
house rules, agent order and the update pin all come from the Portal.
"""
from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UPDATE_REPO = "neelsatyavolu/infocus-editor"
BACKEND_NAMES = ("claude", "codex", "grok")
# Until the Portal sends its agentOrder: Codex (gpt-6.1-sol low) first, then Claude (Sonnet 5.5 high).
DEFAULT_AGENTS = ("codex", "claude", "grok")

Keychain = Callable[[str], "str | None"]


class ConfigError(Exception):
    """A setting or secret is missing; the message says how to fix it."""


def read_keychain(name: str) -> str | None:
    from editor.keychain import Keychain as _Keychain

    return _Keychain().get(name)


def _version(repo: Path = REPO_ROOT) -> str:
    """The release tag (`git describe --tags`), else the package version."""
    try:
        proc = subprocess.run(["git", "-C", str(repo), "describe", "--tags", "--always"],
                              capture_output=True, text=True, check=False, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()[:40]
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        return metadata.version("infocus-editor")
    except metadata.PackageNotFoundError:
        return "0.0.0"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    portal_url: str
    package_dir: Path
    work_root: Path
    agents: tuple[str, ...]
    max_turns: int
    max_budget_usd: float | None
    run_timeout_minutes: int
    keep_days: int
    version: str
    repo_root: Path
    update_repo: str
    auto_update: bool
    require_signed_tags: bool
    machine_kind: str = "dedicated"  # "personal": someone's own computer, no never-sleep settings
    editor_token: str | None = field(default=None, repr=False)

    @property
    def skill_dir(self) -> Path:
        return self.repo_root / ".claude" / "skills" / "infocus-show"

    @property
    def house_rules_example(self) -> Path:
        return self.repo_root / "house-rules.example.md"

    @property
    def codex_home(self) -> Path:
        return self.data_dir / "codex-home"

    @property
    def grok_home(self) -> Path:
        return self.data_dir / "grok-home"

    @property
    def claude_config_dir(self) -> Path:
        return self.data_dir / "claude-config"

    @staticmethod
    def data_dir_from(env: Mapping[str, str]) -> Path:
        raw = env.get("INFOCUS_EDITOR_HOME") or str(Path.home() / "InFocusEditor")
        return Path(raw).expanduser()

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None, keychain: Keychain = read_keychain) -> "Config":
        env = os.environ if env is None else env
        data_dir = cls.data_dir_from(env)
        settings = _read_toml(data_dir / "config.toml")
        if not settings.get("portal_url"):
            raise ConfigError(f"config.toml needs `portal_url` ({data_dir / 'config.toml'}); "
                              "open InFocus Editor Setup and choose Repair.")
        agents = tuple(str(a) for a in settings.get("agents", DEFAULT_AGENTS))
        unknown = [a for a in agents if a not in BACKEND_NAMES]
        if unknown or not agents:
            raise ConfigError(f"config.toml `agents` must list some of {', '.join(BACKEND_NAMES)}")
        budget = settings.get("max_budget_usd")
        kind = str(settings.get("machine_kind", "dedicated"))
        if kind not in ("dedicated", "personal"):
            raise ConfigError('config.toml `machine_kind` must be "dedicated" or "personal"')
        return cls(
            data_dir=data_dir,
            portal_url=str(settings["portal_url"]).rstrip("/"),
            package_dir=Path(str(settings.get("package_dir", data_dir / "package"))).expanduser(),
            work_root=Path(str(settings.get("work_root", data_dir / "jobs"))).expanduser(),
            agents=agents,
            max_turns=int(settings.get("max_turns", 300)),
            max_budget_usd=float(budget) if budget is not None else None,
            run_timeout_minutes=int(settings.get("run_timeout_minutes", 75)),
            keep_days=int(settings.get("keep_days", 14)),
            version=_version(),
            repo_root=REPO_ROOT,
            update_repo=str(settings.get("update_repo", DEFAULT_UPDATE_REPO)),
            # Only the production checkout (<data dir>/app) updates itself, never a dev clone.
            auto_update=bool(settings.get("auto_update", REPO_ROOT == data_dir / "app")),
            require_signed_tags=bool(settings.get("require_signed_tags", False)),
            machine_kind=kind,
            editor_token=keychain("editor_token") or None,
        )


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError(f"No config.toml at {path}. Open InFocus Editor Setup to set this Mac up.")
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml is not valid TOML: {e}") from e
