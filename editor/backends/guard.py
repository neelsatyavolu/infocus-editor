"""Commands and paths the editing agent may never touch: root, the Keychain,
launchd, system folders, and the editor's own installer and updater. Applied
by every backend that supports rules (Claude's can_use_tool, Grok's --deny,
Codex's execpolicy rules)."""
from __future__ import annotations

import re
from pathlib import Path

_SYSTEM_DIR = r"['\"]?/(?:private/)?(?:Library|etc|System|usr)/"
_BASH_DENY = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bsudo\b",
    r"infocus-root",
    r"osascript[\s\S]*administrator",
    r"\blaunchctl\b",
    r"(?:^|[\s;&|(`])security\s+[\w-]*(?:password|keychain|certificate|identity)",
    r"(?:^|[\s;&|(`])(?:pmset|softwareupdate|systemsetup|dscl|csrutil|spctl|nvram)\b",
    r"(?:^|[\s;&|(`])installer\s+-",
    r"(?:install|user-setup|bootstrap-root|uninstall)\.sh",
    r"editor[./](?:updater|pairing|connect)",
    r"(?:codex|grok)-home|claude-config|auth\.json",
    r">>?\s*" + _SYSTEM_DIR,
    r"\b(?:cp|mv|rm|ln|install|mkdir|touch|chmod|chown|chgrp|tee|rsync|ditto|dd|truncate|unzip|tar)\b[^;&|]*\s"
    + _SYSTEM_DIR,
    r"\bsed\b[^;&|]*\s-i[^;&|]*\s" + _SYSTEM_DIR,
))
_SYSTEM_PREFIXES = ("/Library/", "/etc/", "/private/etc/", "/System/", "/usr/", "/opt/homebrew/", "/Applications/")

# Command prefixes for CLIs whose rules match the start of a command.
DENIED_PREFIXES = (
    ("sudo",), ("launchctl",), ("osascript",), ("security",), ("pmset",), ("softwareupdate",),
    ("installer",), ("systemsetup",), ("dscl",), ("/Library/InFocusEditor/bin/infocus-root",),
    ("infocus-root",),
)


def bash_denied(command: str) -> bool:
    return any(p.search(command) for p in _BASH_DENY)


def write_denied(file_path: str, repo_root: Path, data_dir: Path | None = None) -> bool:
    path = Path(file_path).expanduser()
    resolved = str(path.resolve(strict=False)) + ("/" if path.is_dir() else "")
    raw = str(path)
    if any(raw.startswith(p) or resolved.startswith(p) for p in _SYSTEM_PREFIXES):
        return True
    protected = [repo_root / d for d in ("editor", "install", "launchd", ".git")]
    protected.append(Path.home() / "Library" / "LaunchAgents")
    if data_dir is not None:
        protected += [data_dir / "codex-home", data_dir / "grok-home", data_dir / "claude-config",
                      data_dir / "app", data_dir / "config.toml"]
    target = Path(resolved)
    return any(target == p or p in target.parents for p in protected)


def grok_deny_rules() -> list[str]:
    """Grok permission deny rules (`--deny`), which win even in bypassPermissions."""
    rules = [f"Bash({' '.join(prefix)} *)" for prefix in DENIED_PREFIXES]
    rules += [f"Bash({' '.join(prefix)})" for prefix in DENIED_PREFIXES]
    return rules


def codex_rules() -> str:
    """Codex execpolicy rules ($CODEX_HOME/rules/*.rules)."""
    lines = ["# Written by infocus-editor. The editing agent never uses root or system settings."]
    for prefix in DENIED_PREFIXES:
        pattern = ", ".join(f'"{token}"' for token in prefix)
        lines.append(f'prefix_rule(pattern=[{pattern}], decision="forbidden", '
                     'justification="The unattended editor may not change the system.")')
    return "\n".join(lines) + "\n"
