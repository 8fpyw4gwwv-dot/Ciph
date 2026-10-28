"""Generation jobs (jobs_dir/<job_id>.json) and the single-job file lock."""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# queued -> running_cli -> done
#        -> needs_computer_use -> done
#  any open state -> failed | cancelled | expired
OPEN_STATES = {"queued", "running_cli", "needs_computer_use"}
ACTIVE_STATES = {"running_cli", "needs_computer_use"}

STATUS_TEXT = {
    "queued": "wartet in der Warteschlange",
    "running_cli": "wird gerade per Kommandozeile erzeugt",
    "needs_computer_use": "wartet auf Export per Bildschirmsteuerung",
    "done": "fertig",
    "failed": "fehlgeschlagen",
    "cancelled": "abgebrochen",
    "expired": "abgelaufen (Zeitlimit)",
}


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _iso(t: dt.datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def _parse(t: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(t) if t else None


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the file open for a moment
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


@dataclass
class Job:
    job_id: str
    z_code: str
    title: str
    date_from: str
    date_to: str
    expected_file: str
    created_at: str
    status: str = "queued"
    mode: str | None = None
    deadline: str | None = None
    force_during_opening: bool = False
    error: str | None = None
    result_file: str | None = None
    row_count: int | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES

    def note(self, event: str, **info) -> None:
        self.history.append({"ts": _iso(_now()), "event": event, **info})

    def public(self) -> dict:
        d = {
            "job_id": self.job_id,
            "z_code": self.z_code,
            "title": self.title,
            "period": f"{self.date_from} – {self.date_to}",
            "status": self.status,
            "status_text": STATUS_TEXT.get(self.status, self.status),
            "mode": self.mode,
            "expected_file": self.expected_file,
            "created_at": self.created_at,
            "deadline": self.deadline,
        }
        if self.error:
            d["error"] = self.error
        if self.result_file:
            d["result_file"] = self.result_file
        if self.row_count is not None:
            d["row_count"] = self.row_count
        return d


def new_job_id(now: dt.datetime | None = None) -> str:
    now = now or _now()
    return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class JobStore:
    def __init__(self, jobs_dir: Path):
        self.dir = Path(jobs_dir)

    def path(self, job_id: str) -> Path:
        if not job_id or any(c in job_id for c in "/\\:.") or len(job_id) > 64:
            raise KeyError(f"Ungültige Job-ID: {job_id!r}")
        return self.dir / f"{job_id}.json"

    def save(self, job: Job) -> Job:
        write_json_atomic(self.path(job.job_id), asdict(job))
        return job

    def get(self, job_id: str) -> Job | None:
        p = self.path(job_id)
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as fh:
            return Job(**json.load(fh))

    def all(self) -> list[Job]:
        jobs = []
        if not self.dir.exists():
            return jobs
        for p in self.dir.glob("*.json"):
            if p.name in ("cli_status.json", "generation.lock.json"):
                continue
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
                if "job_id" in data:
                    jobs.append(Job(**data))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(jobs, key=lambda j: j.created_at)

    def queued(self) -> list[Job]:
        return [j for j in self.all() if j.status == "queued"]


class GenerationLock:
    """At most one report generation at a time, across processes.

    The lock file is created with O_EXCL. A lock whose deadline has passed is stale
    and may be taken over (the job it belonged to is then expired by the caller).
    """

    CORRUPT_GRACE_SEC = 60  # a just-created lock file may still be empty
    _mutexes: dict[str, threading.Lock] = {}
    _mutexes_guard = threading.Lock()

    def __init__(self, jobs_dir: Path):
        self.path = Path(jobs_dir) / "generation.lock"
        key = str(self.path.resolve()).lower()
        with self._mutexes_guard:
            self._mutex = self._mutexes.setdefault(key, threading.Lock())

    def holder(self) -> dict | None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 0
            return {"job_id": None, "deadline": None, "corrupt": True, "age_sec": age}

    def is_stale(self, info: dict | None, now: dt.datetime | None = None) -> bool:
        if not info:
            return False
        if info.get("corrupt"):
            return info.get("age_sec", 0) > self.CORRUPT_GRACE_SEC
        dl = _parse(info.get("deadline"))
        return dl is None or dl < (now or _now())

    def acquire(self, job_id: str, deadline: dt.datetime, now: dt.datetime | None = None) -> tuple[bool, dict | None]:
        """Returns (acquired, stale_holder_taken_over). On failure the second value is None."""
        with self._mutex:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            taken_over = None
            info = self.holder()
            if info and info.get("job_id") == job_id:
                self._write(job_id, deadline, exclusive=False)
                return True, None
            if info and self.is_stale(info, now):
                # Only remove it if it is still the same stale lock (another process may have replaced it).
                if self.holder() == info or info.get("corrupt"):
                    taken_over = info
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
            try:
                self._write(job_id, deadline, exclusive=True)
            except FileExistsError:
                return False, None
            return True, taken_over

    def extend(self, job_id: str, deadline: dt.datetime) -> None:
        with self._mutex:
            info = self.holder()
            if info and info.get("job_id") == job_id:
                self._write(job_id, deadline, exclusive=False)

    def release(self, job_id: str) -> bool:
        with self._mutex:
            info = self.holder()
            if info and info.get("job_id") == job_id:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                return True
            return False

    def _write(self, job_id: str, deadline: dt.datetime, exclusive: bool) -> None:
        data = json.dumps({"job_id": job_id, "pid": os.getpid(), "since": _iso(_now()),
                           "deadline": _iso(deadline)})
        if exclusive:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
        else:
            write_json_atomic(self.path, json.loads(data))
