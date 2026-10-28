import datetime as dt
import hashlib
import sys
from pathlib import Path

import pytest

from conftest import TZ, Clock, make_cfg
from fixture_data import write_txt
from prokas_mcp import cli as cli_mod
from prokas_mcp.app import App

FAKE = str(Path(__file__).parent / "fake_cli.py")
TEMPLATE = [sys.executable, FAKE, "--liste", "{z_code}", "--von", "{date_from}", "--bis", "{date_to}",
            "--out", "{output_file}"]


def fake_prokas(root: Path) -> Path:
    bw = root / "PROKAS" / "bw"
    bw.mkdir(parents=True)
    (bw / "bwbatch.cmd").write_text(
        "@echo off\r\nrem Berichtswesen headless: Listennr, Berichtszeitraum, Ausgabedatei\r\n"
        "java -cp bw.jar BwStart -liste %1 -von %2 -bis %3 -out %4\r\n", encoding="cp1252")
    (bw / "sicherung.cmd").write_text("rem Berichtswesen backup\r\nxcopy -liste x -out y\r\n", encoding="cp1252")
    (bw / "nacht.bat").write_text("rem Berichtswesen\r\ndel *.tmp\r\nrun -liste 1 -out x\r\n", encoding="cp1252")
    (bw / "liesmich.txt").write_text("Berichtswesen Listennr Berichtszeitraum", encoding="cp1252")
    (bw / "bwjava.jar").write_bytes(b"PK\x03\x04fake")
    return root / "PROKAS"


def snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_discovery_is_read_only_and_skips_unsafe(tmp_path):
    root = fake_prokas(tmp_path)
    before = snapshot(root)
    cfg = make_cfg(tmp_path, prokas_search_dirs=[str(root)])
    cands, skipped, scanned = cli_mod.discover(cfg)
    assert snapshot(root) == before
    assert [Path(c.path).name for c in cands] == ["bwbatch.cmd"]
    assert cands[0].template[-8:] == ["-liste", "{z_code}", "-von", "{date_from}", "-bis", "{date_to}",
                                      "-out", "{output_file}"]
    assert {Path(s["path"]).name for s in skipped} >= {"sicherung.cmd", "nacht.bat"}
    assert cli_mod.detect_bw_install_dir(cfg).endswith("bw")


def test_probe_with_manual_template_and_cache(tmp_path):
    root = fake_prokas(tmp_path)
    before = snapshot(root)
    cfg = make_cfg(tmp_path, prokas_search_dirs=[str(root)], cli={"template": TEMPLATE})
    app = App(cfg, clock=Clock())
    st = app.probe_cli()
    assert st["status"] == "works" and st["works_with"] == "config.yaml"
    assert (cfg.jobs_dir / "cli_status.json").exists()
    assert list((cfg.jobs_dir / "probe").glob("Z0327_probe_*.txt"))
    assert snapshot(root) == before  # nothing written into PROKAS folders
    assert app.probe_cli()["cached"] is True
    assert app.lock.holder() is None


def test_probe_reruns_after_prokas_update(tmp_path, monkeypatch):
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("FAKE_CLI_CALLS", str(calls))
    root = fake_prokas(tmp_path)
    cfg = make_cfg(tmp_path, prokas_search_dirs=[str(root)], cli={"template": TEMPLATE})
    app = App(cfg, clock=Clock())
    app.probe_cli()
    assert calls.read_text().split() == ["Z0327"]
    (root / "bw" / "bwjava.jar").write_bytes(b"PK\x03\x04new version")  # PROKAS update
    r = app.run_report("Z0245")
    assert r["mode"] == "cli"
    assert calls.read_text().split() == ["Z0327", "Z0327", "Z0245"]  # re-probed with Z0327 first


def test_probe_waits_outside_allowed_windows(tmp_path, monkeypatch):
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv("FAKE_CLI_CALLS", str(calls))
    cfg = make_cfg(tmp_path, cli={"template": TEMPLATE})
    app = App(cfg, clock=Clock(dt.datetime(2026, 10, 27, 23, 30, tzinfo=TZ)))
    st = app.probe_cli(force=True)
    assert st["status"] == "untested" and "Nachtjob" in st["error"]
    assert not calls.exists()


def test_probe_not_found(app):
    st = app.probe_cli(force=True)
    assert st["status"] == "not_found"
    assert "Bildschirmsteuerung" in st["status_text"]


def test_run_template_refuses_unsafe_commands(tmp_path):
    with pytest.raises(cli_mod.CliError, match="datenändernde"):
        cli_mod.run_template(["cmd.exe", "/c", "C:\\PROKAS7\\loeschen.cmd", "{z_code}"], "Z0327",
                             dt.date.today(), dt.date.today(), tmp_path / "x.txt", 5, tmp_path)


def test_validate_mapping_good_file(app, fresh):
    write_txt("Z0127", app.cfg.export_dir, "Z0127_nacht.txt", mtime=fresh)
    r = app.validate_mapping("Z0127")
    res = r["results"][0]
    assert res["ok"] and res["rows"] == 3 and not res["missing_required"]
    assert res["format"]["encoding"] == "windows-1252" and res["format"]["separator"] == "Tab"
    assert res["matched"]["ek"] == "Listen-EK"
    assert len(res["sample_rows"]) == 3
    files = sorted(p.name for p in app.cfg.outbox_dir.iterdir())
    assert any(f.endswith(".yaml") for f in files) and any(f.endswith(".md") for f in files)
    assert (app.cfg.log_dir / "mapping_validation.json").exists()


def test_validate_mapping_unknown_headers_get_suggestions(app, fresh):
    p = write_txt("Z0322", app.cfg.export_dir, "Z0322_nacht.txt", mtime=fresh)
    text = p.read_bytes().decode("cp1252").replace("\tPZN\t", "\tP Z N\t").replace("PZN\t", "Pharmazentral-Nr\t", 1)
    p.write_bytes(text.encode("cp1252"))
    r = app.validate_mapping("Z0322")["results"][0]
    assert not r["ok"] and "pzn" in r["missing_required"]
    assert "Pharmazentral-Nr" in r["unmatched_headers"]
    md = next(app.cfg.outbox_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "passt noch nicht" in md


def test_validate_mapping_rejects_paths_outside_export_dir(app):
    from prokas_mcp.app import UserError

    with pytest.raises(UserError):
        app.validate_mapping("Z0127", "..\\..\\Windows\\win.ini")


def test_validate_all_lists_reports_missing_samples(app, fresh):
    write_txt("Z0015", app.cfg.export_dir, "Z0015_nacht.txt", mtime=fresh)
    r = app.validate_mapping(write_proposal=False)
    assert len(r["results"]) == 1 and "Z0016" in r["lists_without_sample"]
    assert list(app.cfg.outbox_dir.iterdir()) == []


def test_freshness_and_nightly_set(app, clock):
    for z in app.cfg.nightly_set[:-1]:
        write_txt(z, app.cfg.export_dir, f"{z}_nacht.txt", mtime=clock.now - dt.timedelta(hours=5))
    write_txt("Z0327", app.cfg.export_dir, "Z0327_alt.txt", mtime=clock.now - dt.timedelta(hours=50))
    r = app.report_freshness()
    assert not r["nightly_complete"] and "Z0327" in r["message"]
    write_txt("Z0327", app.cfg.export_dir, "Z0327_neu.txt", mtime=clock.now - dt.timedelta(hours=2))
    assert app.report_freshness()["nightly_complete"]
