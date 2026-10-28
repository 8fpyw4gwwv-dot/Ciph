"""Audit log: one JSON line per tool call. Never contains row contents."""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

_MAX_PARAM_LEN = 200


def _safe_param(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return f"<{len(v)} Einträge>"
    if isinstance(v, dict):
        return f"<{len(v)} Felder>"
    if isinstance(v, str) and len(v) > _MAX_PARAM_LEN:
        return v[:_MAX_PARAM_LEN] + "…"
    return v


class AuditLog:
    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.log_dir / f"audit-{dt.date.today():%Y-%m}.jsonl"

    def log(self, tool: str, params: dict, *, status: str, mode: str | None = None,
            files: list[str] | None = None, row_count: int | None = None,
            redactions: dict | None = None, duration_ms: int | None = None,
            error: str | None = None, job_id: str | None = None) -> None:
        entry = {
            "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "tool": tool,
            "params": {k: _safe_param(v) for k, v in (params or {}).items()},
            "status": status,
            "mode": mode,
            "job_id": job_id,
            "files": files or [],
            "row_count": row_count,
            # Column names only - values never reach this log.
            "redactions": redactions or {},
            "duration_ms": duration_ms,
        }
        if error:
            entry["error"] = error[:500]
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
