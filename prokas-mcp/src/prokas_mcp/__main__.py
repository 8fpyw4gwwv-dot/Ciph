"""Entry point.

  python -m prokas_mcp            MCP server over stdio (what Claude Desktop starts)
  python -m prokas_mcp check      Self-test for humans: config, folders, rules, sample files
  python -m prokas_mcp validate [Z0127] [datei.xlsx]
  python -m prokas_mcp probe      CLI probe (force)
  python -m prokas_mcp config     Print the claude_desktop_config.json entry
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _print(s: str = "") -> None:
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode())


def _ok(flag: bool) -> str:
    return "[OK]  " if flag else "[!!]  "


def cmd_check() -> int:
    from .app import App
    from .cli import STATUS_TEXT
    from .config import load_config
    from .guards import DAY_NAMES, active_block

    _print("PROKAS-MCP Selbsttest")
    _print("=" * 60)
    try:
        cfg = load_config()
    except Exception as e:
        _print(f"[!!]  config.yaml fehlerhaft: {e}")
        return 1
    _print(f"Konfiguration: {cfg.source}")
    bad = 0
    for p in cfg.ensure_dirs():
        _print(f"[!!]  {p}")
        bad += 1
    for label in ("export_dir", "outbox_dir", "jobs_dir", "log_dir"):
        d = getattr(cfg, label)
        writable = False
        if d.exists():
            probe = d / ".schreibtest"
            try:
                probe.write_text("x", encoding="utf-8")
                probe.unlink()
                writable = True
            except OSError:
                pass
        need_write = label != "export_dir"
        good = d.exists() and (writable or not need_write)
        bad += not good
        _print(f"{_ok(good)}{label:11} {d}" + ("" if good else "  (fehlt oder nicht beschreibbar)"))
    app = App(cfg)
    missing = [z for z in cfg.allowed_z_codes if z not in app.mappings]
    _print(f"{_ok(not missing)}Spaltenzuordnungen: {len(app.mappings)} geladen" + (f", fehlen: {missing}" if missing else ""))
    bad += bool(missing)
    validated = [z for z, m in app.mappings.items() if m.validated]
    _print(f"      davon an echter Datei bestätigt: {len(validated)} ({', '.join(validated) or 'noch keine'})")
    now = app.now()
    block = active_block(now, cfg, False)
    _print(f"      Jetzt {DAY_NAMES[now.weekday()]} {now:%H:%M}: "
           + (f"Listen-Erzeugung gesperrt ({block[0]})" if block else "Listen-Erzeugung erlaubt"))
    cli = app.cli_status.load()
    status = cli.get("status", "unknown")
    _print(f"      Kommandozeile: {status} – {STATUS_TEXT.get(status, '')}")
    fresh = app.report_freshness()
    _print(f"{_ok(fresh['nightly_complete'])}Nachtset: {fresh['message']}")
    _print("")
    _print("Beispieldateien in export_dir:")
    res = app.validate_mapping(write_proposal=False)
    if not res["results"]:
        _print("      keine – bitte 3–5 Exporte (Dateiname mit Z-Nummer, z. B. Z0127_Ladenhueter.xlsx) ablegen.")
    for r in res["results"]:
        _print(f"{_ok(r['ok'])}{r['z_code']} {r['file']}: "
               + (r.get("error") or f"{r['rows']} Zeilen, Spalten erkannt: {len(r['matched'])}, "
                  f"nicht zugeordnet: {len(r['unmatched_headers'])}"
                  + (f", Umwandlungsfehler: {r['conversion_failures']}" if r.get("conversion_failures") else "")))
    _print("")
    _print("Fertig." if not bad else f"{bad} Problem(e) – siehe [!!] oben und README (Fehlerbehebung).")
    return 0 if not bad else 1


def cmd_validate(args: list[str]) -> int:
    from .app import App
    from .config import load_config

    app = App(load_config())
    z = next((a for a in args if a.upper().startswith("Z") and len(a) <= 6), None)
    f = next((a for a in args if a != z), None)
    res = app.validate_mapping(z, f, write_proposal=True)
    _print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_probe() -> int:
    from .app import App
    from .config import load_config

    app = App(load_config())
    _print(json.dumps(app.probe_cli(force=True), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_config() -> int:
    root = Path(__file__).resolve().parents[2]
    venv_py = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    entry = {"mcpServers": {"prokas": {"command": str(venv_py), "args": ["-m", "prokas_mcp"],
                                       "env": {"PROKAS_MCP_CONFIG": str(root / "config.yaml")}}}}
    _print(json.dumps(entry, indent=2))
    return 0


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0].lower() if args else "serve"
    if cmd in ("serve", "server"):
        from .server import main as serve

        serve()
        return
    handlers = {"check": lambda: cmd_check(), "validate": lambda: cmd_validate(args[1:]),
                "probe": lambda: cmd_probe(), "config": lambda: cmd_config()}
    if cmd not in handlers:
        _print(__doc__)
        sys.exit(2)
    sys.exit(handlers[cmd]())


if __name__ == "__main__":
    main()
