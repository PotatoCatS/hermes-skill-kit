"""Append-only action log for dashboard-side operations.

Kept separate from logs/change-log.md (which records pipeline changes) so the
two sources stay distinguishable. Contains no passwords, cookies or secrets.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .safety import BASE

LOG_PATH = BASE / "logs" / "dashboard-actions.log"
MAX_LINE = 500


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())[:MAX_LINE]


def log_action(action: str, detail: str = "", ok: bool = True) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = (
            f"{datetime.now().isoformat(timespec='seconds')} | "
            f"{_one_line(action) or '-'} | {'OK' if ok else 'FAIL'} | "
            f"{_one_line(detail) or '-'}\n"
        ).encode("utf-8")
        fd = os.open(LOG_PATH, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        # Logging must never break the request path.
        pass
