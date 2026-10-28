"""The connector's engine. server.py only exposes these methods as MCP tools."""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import cli as cli_mod
from .audit import AuditLog
from .config import Config
from .exports import ExportFile, latest_by_code, scan_exports
from .german import format_de_date
from .guards import (BlockedWindowError, Period, PolicyError, active_block, check_time_window, check_z_code,
                     resolve_period)
from .jobs import ACTIVE_STATES, GenerationLock, Job, JobStore, new_job_id
from .mapping import ListMapping, load_mappings, proposal_yaml
from .parser import ParsedReport, ParseError, json_value, parse_export, records_for_output
from .readers import SUPPORTED_EXT, ReadError, cell_text, read_raw
from .redaction import Redactor
from .runbook import render_runbook

QUEUED_MAX_AGE = dt.timedelta(hours=12)


class UserError(Exception):
    """Expected problem with a clear message for the owner (no stack trace needed)."""


@dataclass
class Obtained:
    parsed: ParsedReport | None = None
    mode: str | None = None
    export: ExportFile | None = None
    pending: dict | None = None
    stale: bool = False
    note: str | None = None


class App:
    def __init__(self, cfg: Config, clock: Callable[[], dt.datetime] | None = None,
                 mappings: dict[str, ListMapping] | None = None):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self._clock = clock
        self.mappings = mappings if mappings is not None else load_mappings(cfg.mappings_dir)
        self.redactor = Redactor(cfg.denylist_patterns)
        self.audit = AuditLog(cfg.log_dir)
        self.jobs = JobStore(cfg.jobs_dir)
        self.lock = GenerationLock(cfg.jobs_dir)
        self.cli_status = cli_mod.CliStatusStore(cfg.jobs_dir)
        self._cache: OrderedDict[tuple, ParsedReport] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._job_mutex = threading.RLock()

    # --- basics -------------------------------------------------------------

    def now(self) -> dt.datetime:
        if self._clock:
            t = self._clock()
            return t if t.tzinfo else t.replace(tzinfo=self.tz)
        return dt.datetime.now(self.tz)

    @property
    def cli_output_dir(self) -> Path:
        return self.cfg.jobs_dir / "cli_output"

    def mapping(self, z: str) -> ListMapping:
        m = self.mappings.get(z)
        if not m:
            raise UserError(f"Für {z} gibt es noch keine Spaltenzuordnung (mappings/{z}.yaml).")
        return m

    def parse(self, path: Path, mapping: ListMapping) -> ParsedReport:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size, mapping.z_code)
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        parsed = parse_export(path, mapping, self.redactor)
        with self._cache_lock:
            self._cache[key] = parsed
            while len(self._cache) > 64:
                self._cache.popitem(last=False)
        return parsed

    def exports(self) -> list[ExportFile]:
        return scan_exports([self.cfg.export_dir, self.cli_output_dir], self.tz)

    # --- data responses -----------------------------------------------------

    def data_response(self, parsed: ParsedReport, mode: str, *, export: ExportFile | None = None,
                      job: Job | None = None, stale: bool = False, note: str | None = None,
                      include_rows: bool = True) -> dict:
        mapping = self.mappings.get(parsed.z_code)
        rows, truncated = records_for_output(parsed, self.cfg.max_rows_returned)
        now = self.now()
        age = round((now - dt.datetime.fromtimestamp(parsed.path.stat().st_mtime, self.tz)).total_seconds() / 3600, 1)
        summary = {}
        for f in parsed.fields:
            spec = mapping.fields.get(f) if mapping else None
            if spec and spec.type in ("number", "int"):
                vals = [r[f] for r in parsed.records if isinstance(r.get(f), (int, float))]
                if vals:
                    summary[f"{f}_summe"] = round(sum(vals), 2)
        msg = f"{parsed.z_code} ({mapping.title if mapping else ''}): {parsed.row_count} Zeilen"
        if truncated:
            msg += f", die ersten {self.cfg.max_rows_returned} sind enthalten"
        out: dict[str, Any] = {
            "status": "ok",
            "mode": mode,
            "message": msg + ".",
            "z_code": parsed.z_code,
            "title": mapping.title if mapping else parsed.z_code,
            "period": ({"from": parsed.period[0].isoformat(), "to": parsed.period[1].isoformat()}
                       if parsed.period else (export.public(now)["period"] if export and export.period else None)),
            "file": parsed.path.name,
            "file_age_hours": age,
            "row_count": parsed.row_count,
            "fields": parsed.fields,
            "summary": summary,
            "redactions": parsed.redactions.as_dict(),
            "dropped_rows": parsed.dropped_rows,
            "mapping_validated": parsed.mapping_validated,
            "warnings": list(parsed.warnings),
            "files": [str(parsed.path)],
        }
        if export and export.period and not parsed.period:
            out["period"] = {"from": export.period[0].isoformat(), "to": export.period[1].isoformat()}
        if include_rows:
            out["rows"] = rows
            out["truncated"] = truncated
        if job:
            out["job_id"] = job.job_id
        if stale:
            out["stale"] = True
            out["warnings"].insert(0, f"Daten sind {age} Stunden alt (älter als {self.cfg.stale_after_hours:g} h).")
        if note:
            out["warnings"].insert(0, note)
        return out

    # --- freshness ----------------------------------------------------------

    def _candidates(self, z: str, period: Period, mapping: ListMapping) -> list[tuple[ExportFile, ParsedReport | None]]:
        out = []
        for f in self.exports():
            if f.z_code != z:
                continue
            if period.explicit and mapping.needs_period:
                fp = f.period
                parsed = None
                if fp is None:
                    try:
                        parsed = self.parse(f.path, mapping)
                        fp = parsed.period
                    except (ParseError, ReadError, OSError):
                        continue
                if fp != (period.start, period.end):
                    continue
                out.append((f, parsed))
            else:
                out.append((f, None))
        return out

    def find_export(self, z: str, period: Period, mapping: ListMapping, max_age_h: float | None) -> tuple[Obtained | None, list[str]]:
        """Newest parsable export for z/period, optionally only if younger than max_age_h."""
        problems = []
        now = self.now()
        for f, parsed in self._candidates(z, period, mapping):
            if max_age_h is not None and f.age_hours(now) >= max_age_h:
                continue
            try:
                parsed = parsed or self.parse(f.path, mapping)
            except (ParseError, ReadError, OSError) as e:
                problems.append(f"{f.path.name}: {e}")
                continue
            stale = f.age_hours(now) >= self.cfg.stale_after_hours
            return Obtained(parsed=parsed, mode="file", export=f, stale=stale), problems
        return None, problems

    # --- run_report -----------------------------------------------------------

    def obtain(self, z_code: str, *, period: str | None = None, date_from: str | None = None,
               date_to: str | None = None, force_during_opening: bool = False,
               use_cache: bool = True, allow_stale_fallback: bool = True) -> Obtained:
        self.housekeeping()
        z = check_z_code(z_code, self.cfg)
        mapping = self.mapping(z)
        now = self.now()
        p = resolve_period(now.date(), self.cfg, period=period, date_from=date_from, date_to=date_to,
                           needs_period=mapping.needs_period)
        problems: list[str] = []
        if use_cache:
            found, problems = self.find_export(z, p, mapping, self.cfg.stale_after_hours)
            if found:
                return found
        existing = self._open_job_for(z, p)
        if existing:
            return Obtained(pending=self.job_response(existing))
        try:
            check_time_window(now, self.cfg, force_during_opening)
        except BlockedWindowError as e:
            if allow_stale_fallback:
                old, _ = self.find_export(z, p, mapping, None)
                if old:
                    old.stale = True
                    old.note = f"{e} Deshalb wird der letzte vorhandene Export geliefert."
                    return old
            return Obtained(pending={"status": "blocked", "z_code": z, "message": str(e),
                                     "next_allowed": e.next_allowed.isoformat() if e.next_allowed else None,
                                     "can_force": e.can_force})
        job = self._new_job(z, mapping, p, force_during_opening)
        if problems:
            job.note("unusable_exports", files=problems)
        return Obtained(pending=self._try_start(job))

    def run_report(self, z_code: str, date_from: str | None = None, date_to: str | None = None,
                   period: str | None = None, force_during_opening: bool = False,
                   refresh: bool = False) -> dict:
        got = self.obtain(z_code, period=period, date_from=date_from, date_to=date_to,
                          force_during_opening=force_during_opening, use_cache=not refresh)
        if got.pending:
            return got.pending
        return self.data_response(got.parsed, got.mode, export=got.export, stale=got.stale, note=got.note)

    def _expected_file(self, z: str, p: Period, job_id: str) -> Path:
        return self.cfg.export_dir / f"{z}_{p.start.isoformat()}_{p.end.isoformat()}_{job_id}.xlsx"

    def _new_job(self, z: str, mapping: ListMapping, p: Period, force: bool) -> Job:
        now = self.now()
        job_id = new_job_id(now)
        job = Job(job_id=job_id, z_code=z, title=mapping.title, date_from=format_de_date(p.start),
                  date_to=format_de_date(p.end), expected_file=str(self._expected_file(z, p, job_id)),
                  created_at=now.isoformat(timespec="seconds"), force_during_opening=force)
        job.note("created")
        return self.jobs.save(job)

    def _open_job_for(self, z: str, p: Period) -> Job | None:
        for j in self.jobs.all():
            if j.is_open and j.z_code == z and j.date_from == format_de_date(p.start) and j.date_to == format_de_date(p.end):
                return j
        return None

    def _try_start(self, job: Job) -> dict:
        with self._job_mutex:
            deadline = self.now() + dt.timedelta(minutes=self.cfg.job_timeout_min)
            ok, taken_over = self.lock.acquire(job.job_id, deadline, self.now())
            if taken_over and taken_over.get("job_id"):
                self._expire(taken_over["job_id"], "Sperre abgelaufen und übernommen")
            if not ok:
                job.status = "queued"
                job.note("queued", behind=(self.lock.holder() or {}).get("job_id"))
                self.jobs.save(job)
                return self.job_response(job, promote=False)
            return self._start(job)

    def _start(self, job: Job) -> dict:
        """Lock is held. Try CLI, then fall back to the GUI runbook."""
        mapping = self.mapping(job.z_code)
        status = self._cli_status_for_run()
        if status.get("status") in ("works", "degraded") and status.get("template") and self.cfg.cli.get("enabled", True):
            job.status, job.mode = "running_cli", "cli"
            job.note("cli_start")
            self.jobs.save(job)
            self.lock.extend(job.job_id, self.now() + dt.timedelta(seconds=float(self.cfg.cli["timeout_sec"]) + 60))
            out = self.cli_output_dir / Path(job.expected_file).with_suffix(self.cfg.cli.get("output_ext", ".txt")).name
            try:
                cli_mod.run_template(status["template"], job.z_code, _d(job.date_from), _d(job.date_to), out,
                                     float(self.cfg.cli["timeout_sec"]), self.cli_output_dir)
                parsed = self.parse(out, mapping)
            except (cli_mod.CliError, ParseError, ReadError, OSError) as e:
                self._cli_failed(status, str(e))
                job.note("cli_failed", error=str(e))
                job.error = f"Kommandozeile fehlgeschlagen ({e}) – weiter per Bildschirmsteuerung."
            else:
                self._cli_succeeded(status)
                job.status, job.result_file, job.row_count = "done", str(out), parsed.row_count
                job.note("done", mode="cli")
                self.jobs.save(job)
                self.lock.release(job.job_id)
                resp = self.data_response(parsed, "cli", job=job)
                resp.update(self._next_hint())
                return resp
        job.status, job.mode = "needs_computer_use", "gui"
        deadline = self.now() + dt.timedelta(minutes=self.cfg.job_timeout_min)
        job.deadline = deadline.isoformat(timespec="seconds")
        job.note("needs_computer_use")
        self.jobs.save(job)
        self.lock.extend(job.job_id, deadline)
        return self.job_response(job, promote=False)

    def runbook_for(self, job: Job) -> str:
        mapping = self.mappings.get(job.z_code)
        return render_runbook(
            job_id=job.job_id, z_code=job.z_code, title=job.title, date_from=job.date_from, date_to=job.date_to,
            expected_file=job.expected_file, expected_file_txt=str(Path(job.expected_file).with_suffix(".txt")),
            deadline=_hhmm(job.deadline), timeout_min=str(self.cfg.job_timeout_min),
            gui_notes=mapping.gui_notes if mapping else "",
        )

    def job_response(self, job: Job, promote: bool = True) -> dict:
        if promote and job.status == "queued":
            promoted = self._maybe_promote(job)
            if promoted is not None:
                return promoted
            job = self.jobs.get(job.job_id) or job
        out: dict[str, Any] = {"status": job.status, "job_id": job.job_id, "job": job.public()}
        if job.status == "needs_computer_use":
            out.update({
                "message": (f"{job.z_code} muss per Bildschirmsteuerung in PROKAS exportiert werden. "
                            f"Runbook unten Schritt für Schritt ausführen, danach ingest_export('{job.job_id}')."),
                "expected_file": job.expected_file,
                "runbook": self.runbook_for(job),
                "mode": "gui",
            })
            if job.error:
                out["cli_error"] = job.error
        elif job.status == "queued":
            q = [j.job_id for j in self.jobs.queued()]
            holder = self.lock.holder() or {}
            pos = q.index(job.job_id) + 1 if job.job_id in q else "?"
            block = active_block(self.now(), self.cfg, job.force_during_opening)
            if holder.get("job_id"):
                why = f"Es läuft schon ein Auftrag ({holder['job_id']})."
            elif block:
                why = f"Gerade Sperrzeit ({block[0]})."
            else:
                why = "Ein anderer Auftrag ist zuerst dran."
            out["message"] = (f"{why} {job.z_code} wartet in der Warteschlange auf Platz {pos}. "
                              f"Später job_status('{job.job_id}') aufrufen.")
            out["queue_position"] = q.index(job.job_id) + 1 if job.job_id in q else None
        elif job.status == "done":
            out["message"] = f"Fertig. ingest_export('{job.job_id}') liefert die Daten erneut."
        else:
            out["message"] = f"Auftrag {job.job_id}: {job.public()['status_text']}." + (f" {job.error}" if job.error else "")
        return out

    def _maybe_promote(self, job: Job) -> dict | None:
        with self._job_mutex:
            queue = self.jobs.queued()
            if not queue or queue[0].job_id != job.job_id:
                return None
            holder = self.lock.holder()
            if holder and not self.lock.is_stale(holder, self.now()):
                return None
            mapping = self.mapping(job.z_code)
            p = Period(_d(job.date_from), _d(job.date_to), explicit=True)
            found, _ = self.find_export(job.z_code, p, mapping, self.cfg.stale_after_hours)
            if found:
                job.status, job.result_file, job.row_count = "done", str(found.parsed.path), found.parsed.row_count
                job.note("done", mode="file")
                self.jobs.save(job)
                return self.data_response(found.parsed, "file", export=found.export, job=job)
            if active_block(self.now(), self.cfg, job.force_during_opening):
                return None
            return self._try_start(job)

    def _next_hint(self) -> dict:
        q = self.jobs.queued()
        if not q:
            return {}
        return {"next_job": q[0].job_id,
                "next_job_hint": f"Nächster Auftrag in der Warteschlange: job_status('{q[0].job_id}') ({q[0].z_code})."}

    def _expire(self, job_id: str, reason: str) -> None:
        job = self.jobs.get(job_id)
        if job and job.is_open:
            job.status, job.error = "expired", reason
            job.note("expired", reason=reason)
            self.jobs.save(job)
        self.lock.release(job_id)

    def housekeeping(self) -> None:
        now = self.now()
        holder = self.lock.holder()
        for j in self.jobs.all():
            if j.status in ACTIVE_STATES and j.deadline and dt.datetime.fromisoformat(j.deadline) < now:
                self._expire(j.job_id, "Zeitlimit überschritten")
            elif j.status in ACTIVE_STATES and (not holder or holder.get("job_id") != j.job_id) and j.status == "running_cli":
                self._expire(j.job_id, "Kommandozeilen-Lauf wurde unterbrochen")
            elif j.status == "queued" and now - dt.datetime.fromisoformat(j.created_at) > QUEUED_MAX_AGE:
                self._expire(j.job_id, "zu lange in der Warteschlange")
        holder = self.lock.holder()
        if holder and self.lock.is_stale(holder, now):
            self._expire(holder.get("job_id") or "", "Sperre abgelaufen")
            if holder.get("job_id") is None:
                try:
                    self.lock.path.unlink()
                except FileNotFoundError:
                    pass

    # --- jobs ---------------------------------------------------------------

    def get_job(self, job_id: str) -> Job:
        try:
            job = self.jobs.get(str(job_id).strip())
        except KeyError as e:
            raise UserError(str(e)) from e
        if not job:
            raise UserError(f"Auftrag {job_id!r} gibt es nicht. list_jobs() zeigt alle Aufträge.")
        return job

    def job_status(self, job_id: str) -> dict:
        self.housekeeping()
        return self.job_response(self.get_job(job_id))

    def list_jobs(self, include_finished: bool = False) -> dict:
        self.housekeeping()
        jobs = self.jobs.all()
        if not include_finished:
            cutoff = self.now() - dt.timedelta(days=2)
            jobs = [j for j in jobs if j.is_open or dt.datetime.fromisoformat(j.created_at) > cutoff]
        holder = self.lock.holder()
        return {
            "status": "ok",
            "active_job": holder.get("job_id") if holder else None,
            "queue": [j.job_id for j in self.jobs.queued()],
            "jobs": [j.public() for j in reversed(jobs)][:50],
            "message": (f"{sum(j.is_open for j in jobs)} offene Aufträge." if jobs else "Keine Aufträge."),
        }

    def cancel_job(self, job_id: str, reason: str = "") -> dict:
        with self._job_mutex:
            job = self.get_job(job_id)
            if not job.is_open:
                return {"status": job.status, "job_id": job.job_id, "message": f"Auftrag ist bereits {job.public()['status_text']}."}
            job.status = "cancelled"
            job.error = reason or "abgebrochen"
            job.note("cancelled", reason=reason)
            self.jobs.save(job)
            self.lock.release(job.job_id)
        out = {"status": "cancelled", "job_id": job.job_id, "message": f"Auftrag {job.job_id} ({job.z_code}) abgebrochen."}
        out.update(self._next_hint())
        return out

    # --- ingest -------------------------------------------------------------

    def _job_file(self, job: Job) -> Path | None:
        exp = Path(job.expected_file)
        if exp.exists():
            return exp
        for ext in sorted(SUPPORTED_EXT):
            alt = exp.with_suffix(ext)
            if alt.exists():
                return alt
        created = dt.datetime.fromisoformat(job.created_at)
        for f in self.exports():  # newest first
            if f.z_code == job.z_code and f.modified >= created - dt.timedelta(minutes=1) and f.path.parent == exp.parent:
                return f.path
        return None

    def ingest_export(self, job_id: str, wait_sec: float | None = None) -> dict:
        job = self.get_job(job_id)
        mapping = self.mapping(job.z_code)
        if job.status == "done" and job.result_file and Path(job.result_file).exists():
            parsed = self.parse(Path(job.result_file), mapping)
            return self.data_response(parsed, "file", job=job)
        if job.status == "queued":
            return self.job_response(job)
        if job.status == "cancelled":
            raise UserError(f"Auftrag {job.job_id} wurde abgebrochen ({job.error}). Bitte run_report neu starten.")
        wait = self.cfg.ingest_wait_sec if wait_sec is None else min(float(wait_sec), self.cfg.ingest_wait_sec)
        poll = max(0.05, self.cfg.ingest_poll_sec)
        end = time.monotonic() + wait
        path, last_size = None, -1
        while True:
            path = self._job_file(job)
            if path:
                size = path.stat().st_size
                if size > 0 and size == last_size:
                    break  # stable: Excel finished writing
                last_size = size
            if time.monotonic() >= end:
                break
            time.sleep(poll)
        if not path:
            return {
                "status": "file_missing",
                "job_id": job.job_id,
                "mode": "gui",
                "message": (f"Die Datei {Path(job.expected_file).name} ist nach {int(wait)} s nicht in "
                            f"{self.cfg.export_dir} angekommen. Bitte Runbook ab Schritt 5 (Speichern unter) "
                            f"wiederholen und genau diesen Dateinamen verwenden, dann erneut ingest_export('{job.job_id}')."),
                "expected_file": job.expected_file,
                "runbook": self.runbook_for(job),
            }
        try:
            parsed = self.parse(path, mapping)
        except (ParseError, ReadError) as e:
            job.error = str(e)
            job.note("parse_failed", file=path.name)
            self.jobs.save(job)
            raise UserError(f"{e} Datei: {path.name}. Falls die falsche Liste gespeichert wurde: "
                            f"Runbook wiederholen. Sonst validate_mapping('{job.z_code}', '{path.name}').") from e
        with self._job_mutex:
            job = self.get_job(job.job_id)
            job.status, job.result_file, job.row_count, job.error = "done", str(path), parsed.row_count, None
            job.note("done", mode="gui", file=path.name)
            self.jobs.save(job)
            self.lock.release(job.job_id)
        resp = self.data_response(parsed, "gui", job=job)
        if path != Path(job.expected_file):
            resp["warnings"].append(f"Datei hieß {path.name} statt {Path(job.expected_file).name}.")
        resp.update(self._next_hint())
        return resp

    # --- CLI ----------------------------------------------------------------

    def _cli_status_for_run(self) -> dict:
        if not self.cfg.cli.get("enabled", True):
            return {"status": "disabled"}
        st = self.cli_status.load()
        try:
            fp = cli_mod.fingerprint(self.cfg)
        except OSError:
            fp = st.get("fingerprint")
        if st.get("status") in ("unknown", "untested") or (st.get("fingerprint") and st.get("fingerprint") != fp):
            st = self.probe_cli(force=True, _lock_held=True)
        return st

    def _cli_failed(self, st: dict, error: str) -> None:
        st = dict(st)
        st["consecutive_failures"] = int(st.get("consecutive_failures", 0)) + 1
        st["status"] = "failed" if st["consecutive_failures"] >= int(self.cfg.cli.get("max_failures", 3)) else "degraded"
        st["last_error"] = error
        st["last_error_at"] = self.now().isoformat(timespec="seconds")
        self.cli_status.save(st)

    def _cli_succeeded(self, st: dict) -> None:
        st = dict(st)
        st.update(status="works", consecutive_failures=0, last_success_at=self.now().isoformat(timespec="seconds"))
        self.cli_status.save(st)

    def probe_cli(self, force: bool = False, force_during_opening: bool = False, _lock_held: bool = False) -> dict:
        now = self.now()
        current = self.cli_status.load()
        fp = cli_mod.fingerprint(self.cfg)
        if not force and current.get("status") not in (None, "unknown", "untested") and current.get("fingerprint") == fp:
            current["cached"] = True
            current.setdefault("message", current.get("status_text"))
            return current
        if not self.cfg.cli.get("enabled", True):
            return self.cli_status.save({"status": "disabled", "tested_at": now.isoformat(timespec="seconds"),
                                         "fingerprint": fp, "template": None})
        candidates, skipped, scanned = cli_mod.discover(self.cfg)
        manual = self.cfg.cli.get("template")
        if manual:
            candidates.insert(0, cli_mod.Candidate("config.yaml", list(manual), 999, "aus config.yaml (cli.template)"))
        result = {
            "tested_at": now.isoformat(timespec="seconds"),
            "fingerprint": fp,
            "prokas_dirs": [str(p) for p in cli_mod.search_roots(self.cfg)],
            "bw_install_dir": cli_mod.detect_bw_install_dir(self.cfg),
            "files_scanned": scanned,
            "candidates": [c.as_dict() for c in candidates[:10]],
            "skipped_unsafe": skipped[:20],
            "template": None,
            "consecutive_failures": 0,
        }
        if not candidates:
            result.update(status="not_found", error="Keine Startdatei mit Listen-/Ausgabeparametern gefunden.")
            return self.cli_status.save(result)
        block = active_block(now, self.cfg, force_during_opening)
        if block:
            result.update(status="untested", error=f"Test verschoben: {block[0]}.")
            return self.cli_status.save(result)
        test_z = self.cfg.cli.get("test_z_code", "Z0327")
        mapping = self.mappings.get(test_z)
        acquired = _lock_held
        if not _lock_held:
            acquired, _prev = self.lock.acquire(f"probe-{new_job_id(now)}", now + dt.timedelta(minutes=10), now)
            if not acquired:
                result.update(status="untested", error="Test verschoben: es läuft gerade ein anderer Auftrag.")
                return self.cli_status.save(result)
            probe_id = self.lock.holder().get("job_id")
        errors = []
        try:
            for c in candidates[:3]:
                out = self.cfg.jobs_dir / "probe" / f"{test_z}_probe_{now:%Y%m%d%H%M%S}.txt"
                try:
                    cli_mod.run_template(c.template, test_z, now.date(), now.date(), out,
                                         float(self.cfg.cli["timeout_sec"]), self.cfg.jobs_dir / "probe")
                    if mapping:
                        self.parse(out, mapping)
                except (cli_mod.CliError, ParseError, ReadError, OSError) as e:
                    errors.append(f"{c.path}: {e}")
                    continue
                result.update(status="works", template=c.template, works_with=c.path, error=None,
                              last_success_at=now.isoformat(timespec="seconds"))
                return self.cli_status.save(result)
        finally:
            if not _lock_held:
                self.lock.release(probe_id)
        result.update(status="failed", error="; ".join(errors)[:1000])
        return self.cli_status.save(result)

    # --- overview tools -----------------------------------------------------

    def report_freshness(self) -> dict:
        now = self.now()
        latest = latest_by_code(self.exports())
        lists, missing, stale = [], [], []
        for z in self.cfg.allowed_z_codes:
            f = latest.get(z)
            entry = {"z_code": z, "title": self.mappings[z].title if z in self.mappings else None,
                     "nightly": z in self.cfg.nightly_set}
            if f:
                entry.update(f.public(now))
                entry["stale"] = f.age_hours(now) >= self.cfg.stale_after_hours
            else:
                entry.update(file=None, stale=True)
            lists.append(entry)
            if z in self.cfg.nightly_set:
                if not f:
                    missing.append(z)
                elif entry["stale"]:
                    stale.append(z)
        warnings = []
        if missing:
            warnings.append(f"Nachtset unvollständig – fehlt ganz: {', '.join(missing)}.")
        if stale:
            warnings.append(f"Nachtset veraltet (> {self.cfg.stale_after_hours:g} h): {', '.join(stale)}.")
        ok = not missing and not stale
        return {
            "status": "ok",
            "nightly_complete": ok,
            "message": "Nachtset vollständig und aktuell." if ok else " ".join(warnings),
            "warnings": warnings,
            "export_dir": str(self.cfg.export_dir),
            "lists": lists,
        }

    def list_available_reports(self) -> dict:
        validation = self._load_validation()
        out = []
        for z in self.cfg.allowed_z_codes:
            m = self.mappings.get(z)
            entry = {"z_code": z, "nightly": z in self.cfg.nightly_set}
            if m:
                entry.update(m.summary())
                if z in validation:
                    entry["last_check"] = validation[z]
            else:
                entry["title"] = None
                entry["warning"] = "keine Spaltenzuordnung"
            out.append(entry)
        return {"status": "ok", "message": f"{len(out)} freigegebene Listen.", "reports": out}

    # --- mapping validation ---------------------------------------------------

    def _validation_path(self) -> Path:
        return self.cfg.log_dir / "mapping_validation.json"

    def _load_validation(self) -> dict:
        import json

        try:
            with open(self._validation_path(), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _resolve_export_name(self, file: str) -> Path:
        name = Path(str(file)).name  # only files inside export_dir / cli_output
        for d in (self.cfg.export_dir, self.cli_output_dir):
            p = d / name
            if p.exists():
                return p
        raise UserError(f"Datei {name!r} liegt nicht in {self.cfg.export_dir}.")

    def validate_mapping(self, z_code: str | None = None, file: str | None = None, write_proposal: bool = True) -> dict:
        from .staging import write_outbox_pair

        targets: list[tuple[str, Path]] = []
        latest = latest_by_code(self.exports())
        if file:
            path = self._resolve_export_name(file)
            z = check_z_code(z_code, self.cfg) if z_code else (next((f.z_code for f in self.exports() if f.path == path), None))
            if not z:
                raise UserError("Bitte z_code angeben – aus dem Dateinamen ist die Liste nicht erkennbar.")
            targets.append((z, path))
        elif z_code:
            z = check_z_code(z_code, self.cfg)
            if z not in latest:
                raise UserError(f"Keine Exportdatei für {z} in {self.cfg.export_dir}. Bitte eine Beispieldatei dort ablegen "
                                f"(Dateiname mit {z}) oder run_report('{z}') ausführen.")
            targets.append((z, latest[z].path))
        else:
            targets = [(z, latest[z].path) for z in self.cfg.allowed_z_codes if z in latest]
        results = []
        validation = self._load_validation()
        for z, path in targets:
            r = self._validate_one(z, path)
            if write_proposal and r.get("proposal_yaml"):
                md = _proposal_md(r)
                files = write_outbox_pair(self.cfg.outbox_dir, f"mapping_vorschlag_{z}", ".yaml", r["proposal_yaml"], md,
                                          encoding="utf-8", newline="\n", now=self.now())
                r["proposal_files"] = files
            r.pop("proposal_yaml", None)
            validation[z] = {"checked_at": self.now().isoformat(timespec="seconds"), "file": path.name,
                             "ok": r["ok"], "missing_required": r.get("missing_required", [])}
            results.append(r)
        if results:
            from .jobs import write_json_atomic

            write_json_atomic(self._validation_path(), validation)
        missing = [z for z in self.cfg.allowed_z_codes if z not in latest]
        ok_count = sum(r["ok"] for r in results)
        return {
            "status": "ok",
            "message": (f"{ok_count} von {len(results)} geprüften Listen passen. "
                        + (f"Ohne Beispieldatei: {', '.join(missing)}." if missing and not z_code and not file else "")),
            "results": results,
            "lists_without_sample": missing if not z_code and not file else [],
            "files": [str(p) for _, p in targets],
        }

    def _validate_one(self, z: str, path: Path) -> dict:
        mapping = self.mapping(z)
        try:
            raw = read_raw(path)
        except ReadError as e:
            return {"z_code": z, "file": path.name, "ok": False, "error": str(e)}
        try:
            parsed = parse_export(path, mapping, self.redactor, raw=raw)
        except ParseError as e:
            from .parser import locate_header

            idx, match = locate_header(raw.rows, mapping)
            r = {"z_code": z, "file": path.name, "ok": False, "error": str(e), "format": raw.describe()}
            if match:
                r.update(self._match_info(match))
                r["proposal_yaml"] = proposal_yaml(mapping, match, path.name)
            else:
                header_guess = next((row for row in raw.rows[:40] if sum(1 for c in row if cell_text(c)) >= 3), [])
                r["headers_found"] = [cell_text(c) for c in header_guess if cell_text(c)]
            return r
        # How many non-empty cells failed to convert, per field (German numbers/dates check).
        from .parser import _is_footer

        header_texts = [cell_text(h) for h in raw.rows[parsed.header_row - 1]]
        data_rows = [row for row in raw.rows[parsed.header_row:]
                     if any(cell_text(c) for c in row) and not _is_footer(row)
                     and [cell_text(c) for c in row[: len(header_texts)]] != header_texts]
        failures: dict[str, int] = {}
        for fname, idx in parsed.match.columns.items():
            spec = mapping.fields[fname]
            if spec.type == "text":
                continue
            bad = 0
            for row in data_rows:
                v = row[idx] if idx < len(row) else None
                if cell_text(v) and spec.convert(v) is None:
                    bad += 1
            if bad:
                failures[fname] = bad
        r = {
            "z_code": z, "file": path.name, "ok": not parsed.match.missing_required and not failures,
            "format": parsed.format, "header_row": parsed.header_row, "rows": parsed.row_count,
            "dropped_rows": parsed.dropped_rows, "period_in_file": (
                f"{parsed.period[0]} – {parsed.period[1]}" if parsed.period else None),
            "conversion_failures": failures,
            "redactions": parsed.redactions.as_dict(),
            "sample_rows": [{k: json_value(v) for k, v in rec.items()} for rec in parsed.records[:3]],
            "mapping_validated_flag": mapping.validated,
        }
        r.update(self._match_info(parsed.match))
        if failures:
            r["hint"] = ("Einige Werte ließen sich nicht umwandeln (Zahl/Datum/PZN). Bei vielen Fehlern stimmt die "
                         "Spaltenzuordnung nicht, bei einzelnen sind es oft Zwischensummen-Zeilen.")
        r["proposal_yaml"] = proposal_yaml(mapping, parsed.match, path.name)
        return r

    def _match_info(self, match) -> dict:
        return {
            "matched": {f: match.headers[i] for f, i in match.columns.items()},
            "unmatched_headers": [h + (" (personenbezogen – wird immer entfernt)" if self.redactor.is_personal(h) else "")
                                  for h in match.unmatched],
            "missing_required": match.missing_required,
            "missing_optional": match.missing_optional,
            "suggestions": match.suggestions,
        }

    # --- status ---------------------------------------------------------------

    def connector_status(self) -> dict:
        self.housekeeping()
        now = self.now()
        dirs = {}
        for label in ("export_dir", "outbox_dir", "jobs_dir", "log_dir"):
            d: Path = getattr(self.cfg, label)
            dirs[label] = {"path": str(d), "exists": d.exists()}
        block = active_block(now, self.cfg, False)
        cli = self.cli_status.load()
        fresh = self.report_freshness()
        validated = [z for z, m in self.mappings.items() if m.validated]
        holder = self.lock.holder()
        problems = [f"{k} fehlt: {v['path']}" for k, v in dirs.items() if not v["exists"]]
        missing_maps = [z for z in self.cfg.allowed_z_codes if z not in self.mappings]
        if missing_maps:
            problems.append(f"Ohne Spaltenzuordnung: {', '.join(missing_maps)}")
        return {
            "status": "ok" if not problems else "problem",
            "message": ("Connector bereit." if not problems else "Probleme: " + "; ".join(problems)),
            "now": now.isoformat(timespec="minutes"),
            "generation_allowed_now": block is None,
            "blocked_by": block[0] if block else None,
            "can_force": block[1] if block else None,
            "cli": {"status": cli.get("status", "unknown"), "text": cli_mod.STATUS_TEXT.get(cli.get("status", "unknown")),
                    "tested_at": cli.get("tested_at")},
            "active_job": holder.get("job_id") if holder else None,
            "queued_jobs": len(self.jobs.queued()),
            "nightly_complete": fresh["nightly_complete"],
            "nightly_warnings": fresh["warnings"],
            "mappings_validated": f"{len(validated)} von {len(self.cfg.allowed_z_codes)}",
            "folders": dirs,
            "config": self.cfg.source,
        }


def _d(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%d.%m.%Y").date()


def _hhmm(iso: str | None) -> str:
    if not iso:
        return "-"
    return dt.datetime.fromisoformat(iso).strftime("%H:%M Uhr (%d.%m.)")


def _proposal_md(r: dict) -> str:
    lines = [f"# Vorschlag Spaltenzuordnung {r['z_code']}", "",
             f"Geprüfte Datei: `{r['file']}`", "",
             "Ergebnis: " + ("**passt**" if r.get("ok") else "**passt noch nicht**"), ""]
    if r.get("error"):
        lines += [f"Fehler: {r['error']}", ""]
    if r.get("matched"):
        lines += ["## Erkannte Spalten", "", "| Feld | Spalte in der Datei |", "| --- | --- |"]
        lines += [f"| {f} | {h} |" for f, h in r["matched"].items()]
        lines.append("")
    if r.get("unmatched_headers"):
        lines += ["## Nicht übernommene Spalten (verlassen den Server nicht)", ""]
        lines += [f"- {h}" for h in r["unmatched_headers"]]
        lines.append("")
    if r.get("suggestions"):
        lines += ["## Ähnliche Namen (bitte prüfen)", ""]
        lines += [f"- `{h}` → {', '.join(s)}" for h, s in r["suggestions"].items()]
        lines.append("")
    if r.get("conversion_failures"):
        lines += ["## Werte, die nicht umgewandelt werden konnten", ""]
        lines += [f"- {f}: {n} Zellen" for f, n in r["conversion_failures"].items()]
        lines.append("")
    lines += ["## Übernehmen", "",
              f"Wenn alles stimmt: die .yaml-Datei als `mappings\\{r['z_code']}.yaml` in den Connector-Ordner kopieren "
              "(alte Datei ersetzen) und Claude Desktop neu starten.", ""]
    return "\n".join(lines)
