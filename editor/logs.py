"""The recent log for the Portal's `logs` command, with secrets scrubbed."""
from __future__ import annotations

import re
from collections import deque
from pathlib import Path

LOG_FILE = Path.home() / "Library" / "Logs" / "infocus-editor.log"
MAX_OUTPUT = 4000

_PATTERNS = (
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "sk-ant-[hidden]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [hidden]"),
    (re.compile(r"(?i)([?&](?:token|code|key|signature|sig)=)[^&\s\"']+"), r"\1[hidden]"),
    (re.compile(r"(?i)(\"?(?:access_token|refresh_token|id_token|token|api_key|apikey|secret|password|"
                r"ciphertext|deviceCode|device_code)\"?\s*[:=]\s*\"?)[^\s\",}]+"), r"\1[hidden]"),
    (re.compile(r"\b(?:rw|r)\.\d{9,}\.[0-9a-f]{32,}\b"), "[hidden file token]"),
    (re.compile(r"\bifd_[A-Za-z0-9_-]{8,}"), "ifd_[hidden]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[hidden jwt]"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"), "[hidden]"),
)


def scrub(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def tail_log(path: Path = LOG_FILE, lines: int = 100) -> str:
    try:
        with path.open("r", errors="replace") as fh:
            last = deque(fh, maxlen=lines)
    except OSError:
        return f"No log at {path}."
    text = scrub("".join(last))
    return text[-MAX_OUTPUT:]
