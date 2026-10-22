"""Mask secrets in anything the editor writes to a log: the global log tail sent
to the Portal, the per-job log, agent transcripts and the debug bundle."""
from __future__ import annotations

import re
import threading

_HIDDEN = "[hidden]"
_FIELDS = (r"access_token|refresh_token|id_token|token|api_key|apikey|secret|password|ciphertext|"
           r"deviceCode|device_code")

_PATTERNS = (
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "sk-ant-[hidden]"),
    (re.compile(r"\bifd_[A-Za-z0-9_-]{8,}"), "ifd_[hidden]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [hidden]"),
    (re.compile(r"(?i)([?&](?:token|code|key|signature|sig)=)[^&\s\"'\\]+"), r"\1[hidden]"),
    # JSON string values, escapes included: "refresh_token": "a b\"c"
    (re.compile(rf"(?i)(?<![A-Za-z0-9_])(\"(?:{_FIELDS})\"\s*:\s*\")(?:[^\"\\]|\\.)*\""), rf'\1{_HIDDEN}"'),
    # JSON inside a JSON string, the whole escaped value: \"password\": \"a b c\"
    (re.compile(rf"(?i)(?<![A-Za-z0-9_])(\\\"(?:{_FIELDS})\\\"\s*:\s*\\\")(?:(?!\\\").)*"), rf"\1{_HIDDEN}"),
    # Unquoted keys: token=abc, password: abc
    (re.compile(rf"(?i)(?<![A-Za-z0-9_])((?:{_FIELDS})\s*[:=]\s*(?:\\?['\"])?)(?!\[hidden\])[^\s\"',}}\\]+"),
     rf"\1{_HIDDEN}"),
    # Quoted keys with a bare value ('token': abc, \"token\": abc). JSON null/true/false and numbers
    # stay, so a transcript line is still valid JSON.
    (re.compile(rf"(?i)(?<![A-Za-z0-9_])((?:{_FIELDS})\\?['\"]\s*[:=]\s*(?:\\?['\"])?)(?!\[hidden\])"
                rf"(?!(?:null|true|false|-?\d+(?:\.\d+)?)(?=[\s,}}\]]|$))[^\s\"',}}\\]+"), rf"\1{_HIDDEN}"),
    (re.compile(r"\b(?:rw|r)\.\d{9,}\.[0-9a-f]{32,}\b"), "[hidden file token]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[hidden jwt]"),
)
# Long opaque strings (keys, base64 blobs) that no rule above names. A run that continues
# an absolute path ("/Users/…/2026-10-21-job/show/analysis") or is made of short
# slash-separated parts ("Storage/Shows/2026-10-21/editor-logs/j1-v1") is a path, not a secret.
_OPAQUE = re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}")

_known: set[str] = set()
_lock = threading.Lock()


def remember_secret(value: str | None) -> None:
    """Mask every exact occurrence of this value from now on (the editor token and the
    Claude OAuth token, as the Keychain hands them out)."""
    if value and len(value) >= 8:
        with _lock:
            _known.add(value)


def forget_secrets() -> None:
    """For tests."""
    with _lock:
        _known.clear()


def _opaque(match: re.Match) -> str:
    run = match.group(0)
    before = match.string[match.start() - 1] if match.start() else ""
    is_path = before in ("/", "~") or ("/" in run and all(len(part) < 40 for part in run.split("/")))
    return run if is_path else _HIDDEN


def scrub(text: str) -> str:
    with _lock:
        known = sorted(_known, key=len, reverse=True)
    for value in known:
        text = text.replace(value, _HIDDEN)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return _OPAQUE.sub(_opaque, text)
