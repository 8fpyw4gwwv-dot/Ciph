import datetime as dt
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

from conftest import TZ, Clock, make_cfg
from fixture_data import write_txt
from prokas_mcp.app import App, UserError
from prokas_mcp.guards import NotAllowedError
from prokas_mcp.jobs import GenerationLock

FAKE = str(Path(__file__).parent / "fake_cli.py")
TEMPLATE = [sys.executable, FAKE, "--liste", "{z_code}", "--von", "{date_from}", "--bis", "{date_to}",
            "--out", "{output_file}"]


def cli_app(tmp_path, clock=None, **cli):
    cfg = make_cfg(tmp_path, cli={"template": TEMPLATE, "timeout_sec": 3, **cli})
    return App(cfg, clock=clock or Clock())


def save_expected(app: App, job_id: str, z: str):
    job = app.jobs.get(job_id)
    p = Path(job.expected_file)
    write_txt(z, p.parent, p.with_suffix(".txt").name)  # agent used "Export in Editor"
    return p


# --- file mode -----------------------------------------------------------------

def test_fresh_export_is_served_without_generation(app, fresh):
    write_txt("Z0127", app.cfg.export_dir, "Z0127_nacht.txt", mtime=fresh)
    r = app.run_report("Z0127")
    assert r["status"] == "ok" and r["mode"] == "file" and r["row_count"] == 3
    assert app.jobs.all() == []
    assert app.lock.holder() is None


def test_explicit_period_must_match_file(app, fresh):
    write_txt("Z0015", app.cfg.export_dir, "Z0015_2026-10-01_2026-10-27_x.txt", mtime=fresh)
    r = app.run_report("Z0015", date_from="01.10.2026", date_to="27.10.2026")
    assert r["mode"] == "file"
    r = app.run_report("Z0015", period="letzte 7 Tage")
    assert r["status"] == "needs_computer_use"


def test_period_read_from_file_preamble(app, fresh):
    write_txt("Z0016", app.cfg.export_dir, "Z0016_nacht.txt", period=("21.10.2026", "27.10.2026"), mtime=fresh)
    assert app.run_report("Z0016", period="letzte 7 Tage")["mode"] == "file"


def test_stale_export_triggers_generation(app, clock):
    write_txt("Z0127", app.cfg.export_dir, "Z0127_alt.txt", mtime=clock.now - dt.timedelta(hours=48))
    r = app.run_report("Z0127")
    assert r["status"] == "needs_computer_use"


def test_stale_export_returned_with_warning_when_blocked(app, clock):
    write_txt("Z0127", app.cfg.export_dir, "Z0127_alt.txt", mtime=clock.now - dt.timedelta(hours=48))
    clock.now = dt.datetime(2026, 10, 27, 23, 0, tzinfo=TZ)
    r = app.run_report("Z0127")
    assert r["status"] == "ok" and r["stale"] is True
    assert "Nachtjob" in r["warnings"][0]


# --- policy ---------------------------------------------------------------------

def test_blocked_window_creates_no_job(app, clock):
    clock.now = dt.datetime(2026, 10, 27, 23, 0, tzinfo=TZ)
    r = app.run_report("Z0245")
    assert r["status"] == "blocked" and not r["can_force"]
    assert app.jobs.all() == []


def test_opening_hours_require_force(app, clock):
    clock.now = dt.datetime(2026, 10, 27, 11, 0, tzinfo=TZ)
    r = app.run_report("Z0245")
    assert r["status"] == "blocked" and r["can_force"]
    r = app.run_report("Z0245", force_during_opening=True)
    assert r["status"] == "needs_computer_use"


def test_not_allowed_list(app):
    with pytest.raises(NotAllowedError):
        app.run_report("Z0145")


# --- CLI -> GUI fallback ----------------------------------------------------------

def test_cli_success(tmp_path):
    app = cli_app(tmp_path)
    r = app.run_report("Z0015", period="letzte 7 Tage")
    assert r["status"] == "ok" and r["mode"] == "cli", r
    assert r["row_count"] == 4
    st = json.loads((app.cfg.jobs_dir / "cli_status.json").read_text(encoding="utf-8"))
    assert st["status"] == "works" and st["template"] == TEMPLATE
    assert app.lock.holder() is None
    assert app.jobs.all()[0].status == "done"
    # Second call: served from the CLI output as a fresh file.
    assert app.run_report("Z0015", period="letzte 7 Tage")["mode"] == "file"


@pytest.mark.parametrize("mode", ["exit", "empty", "garbage", "nofile", "timeout"])
def test_cli_failure_falls_back_to_gui_in_same_call(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLI_FAIL_FOR", "Z0015")
    monkeypatch.setenv("FAKE_CLI_MODE", mode)
    app = cli_app(tmp_path, timeout_sec=1.5 if mode == "timeout" else 5)
    r = app.run_report("Z0015", period="letzte 7 Tage")
    assert r["status"] == "needs_computer_use", r
    assert "Kommandozeile fehlgeschlagen" in r["cli_error"]
    job_id = r["job_id"]
    assert job_id in r["runbook"] and "Z0015" in r["runbook"] and r["expected_file"] in r["runbook"]
    assert "Strg+A" in r["runbook"]
    st = app.cli_status.load()
    assert st["status"] == "degraded" and st["consecutive_failures"] == 1
    assert app.lock.holder()["job_id"] == job_id
    # The agent exports via GUI, then ingests.
    save_expected(app, job_id, "Z0015")
    done = app.ingest_export(job_id)
    assert done["status"] == "ok" and done["mode"] == "gui" and done["row_count"] == 4
    assert app.lock.holder() is None
    assert app.jobs.get(job_id).status == "done"


def test_cli_marked_failed_after_repeated_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_FAIL_FOR", "Z0015,Z0016,Z0245,Z0003")
    monkeypatch.setenv("FAKE_CLI_MODE", "exit")
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("FAKE_CLI_CALLS", str(calls))
    app = cli_app(tmp_path, max_failures=3)
    for z in ("Z0015", "Z0016", "Z0245"):
        r = app.run_report(z)
        assert r["status"] == "needs_computer_use"
        app.cancel_job(r["job_id"])
    assert app.cli_status.load()["status"] == "failed"
    before = calls.read_text().count("\n")
    r = app.run_report("Z0003")
    assert r["status"] == "needs_computer_use" and "cli_error" not in r
    assert calls.read_text().count("\n") == before  # CLI not tried any more


def test_no_cli_found_goes_straight_to_gui(app):
    r = app.run_report("Z0245")
    assert r["status"] == "needs_computer_use" and r["mode"] == "gui"
    assert app.cli_status.load()["status"] == "not_found"


# --- jobs, lock, queue -----------------------------------------------------------

def test_second_request_is_queued_and_promoted(app):
    a = app.run_report("Z0245")
    b = app.run_report("Z0003")
    assert a["status"] == "needs_computer_use"
    assert b["status"] == "queued" and b["queue_position"] == 1
    assert app.job_status(b["job_id"])["status"] == "queued"
    # Same list + period again returns the existing job instead of a new one.
    again = app.run_report("Z0245")
    assert again["job_id"] == a["job_id"]
    save_expected(app, a["job_id"], "Z0245")
    done = app.ingest_export(a["job_id"])
    assert done["next_job"] == b["job_id"]
    promoted = app.job_status(b["job_id"])
    assert promoted["status"] == "needs_computer_use" and "runbook" in promoted
    assert app.lock.holder()["job_id"] == b["job_id"]


def test_cancel_releases_lock(app):
    a = app.run_report("Z0245")
    b = app.run_report("Z0003")
    c = app.cancel_job(a["job_id"], "unbekannter Dialog")
    assert c["status"] == "cancelled" and c["next_job"] == b["job_id"]
    assert app.lock.holder() is None
    assert app.job_status(b["job_id"])["status"] == "needs_computer_use"
    with pytest.raises(UserError, match="abgebrochen"):
        app.ingest_export(a["job_id"])


def test_expired_job_releases_lock(app, clock):
    a = app.run_report("Z0245")
    clock.now += dt.timedelta(minutes=app.cfg.job_timeout_min + 1)
    b = app.run_report("Z0003")
    assert b["status"] == "needs_computer_use"
    assert app.jobs.get(a["job_id"]).status == "expired"
    # A late file for the expired job is still accepted.
    save_expected(app, a["job_id"], "Z0245")
    assert app.ingest_export(a["job_id"])["status"] == "ok"
    assert app.lock.holder()["job_id"] == b["job_id"]  # not released by the other job


def test_lock_is_exclusive_across_threads(tmp_path):
    lock = GenerationLock(tmp_path)
    deadline = dt.datetime.now().astimezone() + dt.timedelta(minutes=5)
    wins = []
    barrier = threading.Barrier(12)

    def worker(i):
        barrier.wait()
        ok, _ = GenerationLock(tmp_path).acquire(f"job{i}", deadline)
        if ok:
            wins.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 1
    assert lock.holder()["job_id"] == f"job{wins[0]}"
    assert not lock.release("someone-else")
    assert lock.release(f"job{wins[0]}")


def test_stale_lock_is_taken_over(tmp_path):
    lock = GenerationLock(tmp_path)
    past = dt.datetime.now().astimezone() - dt.timedelta(minutes=1)
    assert lock.acquire("old", past)[0]
    ok, prev = lock.acquire("new", past + dt.timedelta(hours=1))
    assert ok and prev["job_id"] == "old"


# --- ingest ------------------------------------------------------------------------

def test_ingest_missing_file_gives_clear_hint(app):
    a = app.run_report("Z0245")
    r = app.ingest_export(a["job_id"], wait_sec=0.2)
    assert r["status"] == "file_missing" and "Speichern unter" in r["message"]
    assert app.jobs.get(a["job_id"]).status == "needs_computer_use"


def test_ingest_accepts_differently_named_file(app):
    a = app.run_report("Z0245")
    write_txt("Z0245", app.cfg.export_dir, "Z0245 Defektquote.txt")
    r = app.ingest_export(a["job_id"])
    assert r["status"] == "ok" and any("statt" in w for w in r["warnings"])


def test_ingest_wrong_list_keeps_job_open(app):
    a = app.run_report("Z0245")
    p = Path(app.jobs.get(a["job_id"]).expected_file)
    write_txt("Z0015", p.parent, p.name.replace(".xlsx", ".txt"))
    with pytest.raises(UserError):
        app.ingest_export(a["job_id"])
    assert app.jobs.get(a["job_id"]).is_open
    assert app.lock.holder()["job_id"] == a["job_id"]


def test_ingest_xlsx(app):
    from fixture_data import write_xlsx

    a = app.run_report("Z0105", period="letzter Monat")
    p = Path(a["expected_file"])
    write_xlsx("Z0105", p.parent, p.name)
    r = app.ingest_export(a["job_id"])
    assert r["status"] == "ok" and r["row_count"] == 2 and r["summary"]["lagerwert_summe"] == 50000.0


def test_list_jobs_and_unknown_job(app):
    a = app.run_report("Z0245")
    lst = app.list_jobs()
    assert lst["active_job"] == a["job_id"] and lst["jobs"][0]["job_id"] == a["job_id"]
    with pytest.raises(UserError):
        app.job_status("gibtsnicht")
    with pytest.raises(UserError):
        app.job_status("../../etc/passwd")


def _proc_acquire(path, i, start_at, q):
    import time as _t

    from prokas_mcp.jobs import GenerationLock as GL

    while _t.time() < start_at:
        pass
    ok, _ = GL(Path(path)).acquire(f"proc{i}", dt.datetime.now().astimezone() + dt.timedelta(minutes=5))
    q.put((i, ok))


def test_lock_is_exclusive_across_processes(tmp_path):
    import multiprocessing as mp
    import time as _t

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    start_at = _t.time() + 2.0
    procs = [ctx.Process(target=_proc_acquire, args=(str(tmp_path), i, start_at, q)) for i in range(6)]
    [p.start() for p in procs]
    results = [q.get(timeout=30) for _ in procs]
    [p.join() for p in procs]
    assert sum(ok for _, ok in results) == 1


def test_empty_lock_file_is_respected_briefly(tmp_path):
    lock = GenerationLock(tmp_path)
    lock.path.write_text("")  # another process just created it and has not written yet
    ok, _ = lock.acquire("me", dt.datetime.now().astimezone() + dt.timedelta(minutes=5))
    assert not ok
    old = dt.datetime.now().timestamp() - 120
    import os

    os.utime(lock.path, (old, old))
    ok, prev = lock.acquire("me", dt.datetime.now().astimezone() + dt.timedelta(minutes=5))
    assert ok and prev["corrupt"]
