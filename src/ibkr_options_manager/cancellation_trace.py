"""Local timing trace for app-owned paper bracket cancellation."""

from __future__ import annotations

import json
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from .execution import default_paper_journal_path

_TRACE_LOCK = Lock()
_MAX_TRACE_BYTES = 2_000_000

# Diagnostic correlation only. Order identity and broker decisions never depend on it.
current_cancellation_context: ContextVar[tuple[str, int] | None] = ContextVar(
    "current_cancellation_context", default=None
)
current_snapshot_context: ContextVar[tuple[str, str, int | None] | None] = ContextVar(
    "current_snapshot_context", default=None
)


def cancellation_trace_path() -> Path:
    configured = os.environ.get("IBKR_OPTIONS_MANAGER_CANCEL_TRACE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return default_paper_journal_path().parent / "logs" / "bracket-cancellations.jsonl"


def record_cancellation_event(event: str, **fields: Any) -> bool:
    """Append a bounded, private JSONL event without disrupting an order action."""
    path = cancellation_trace_path()
    payload = {"at_utc": datetime.now(UTC).isoformat(), "event": event, **fields}
    try:
        line = json.dumps(payload, sort_keys=True, default=str) + "\n"
        with _TRACE_LOCK:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size >= _MAX_TRACE_BYTES:
                path.replace(Path(f"{path}.1"))
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(line)
            path.chmod(0o600)
    except (OSError, TypeError, ValueError) as error:
        print(f"Cancellation trace unavailable at {path}: {error}", file=sys.stderr)
        return False
    return True
