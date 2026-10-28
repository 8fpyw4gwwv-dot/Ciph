"""MCP server "prokas": tools and prompts over stdio."""

from __future__ import annotations

import functools
import logging
import threading
import time
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .analysis import Analyses
from .app import App, UserError
from .config import Config, load_config
from .guards import BlockedWindowError, PolicyError, active_block
from .readers import ReadError
from .runbook import render_runbook
from .staging import StagingError, build_order_list, build_price_file, write_outbox_pair

log = logging.getLogger("prokas_mcp")

INSTRUCTIONS = """\
Read-only connector to the pharmacy's PROKAS7 Berichtswesen (easyApotheke im Nedderfeld Center).
Data comes only from Z-list exports; personal data is removed before you see it.
- Start with connector_status() if unsure. run_report(z_code, period) gets a list:
  mode=file (fresh export), mode=cli (headless), or status=needs_computer_use with a runbook.
- needs_computer_use: follow the runbook exactly with computer use in PROKAS (prostartx86.exe),
  then call ingest_export(job_id). Never press Ctrl+A in PROKAS (= Abbrechen).
- Analysis tools (margin_check, lost_sales, ...) fetch what they need and may also return
  needs_computer_use; after ingest_export call the same analysis tool again.
- Generating during opening hours needs force_during_opening=true: only pass it after the owner
  explicitly agreed in this conversation. The Nachtjob window can never be overridden.
- Price files: show margin_check suggestions first; call stage_price_file only for items the owner approved.
- Write tools only create proposal files in the outbox with a .md preview. Nothing is ever
  changed, booked, priced or ordered in PROKAS by this connector.
Answer the owner in German, short and concrete (he reads on an iPhone).
"""

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
GEN = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
STAGE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

Period = Annotated[str | None, Field(description="Zeitraum, z. B. 'letzte 7 Tage', 'letzter Monat', 'Oktober 2026', "
                                                  "'01.10.2026-27.10.2026'. Leer = neuester Export bzw. letzte 30 Tage.")]
Force = Annotated[bool, Field(description="Nur auf ausdrücklichen Wunsch des Inhabers: Liste auch während der Öffnungszeit erzeugen.")]
RefreshOpt = Annotated[bool, Field(description="Auch optionale Listen neu erzeugen, wenn sie fehlen (kann Bildschirmsteuerung auslösen).")]


def build_server(cfg: Config | None = None, app: App | None = None) -> tuple[MCPServer, App]:
    cfg = cfg or load_config()
    problems = cfg.ensure_dirs()
    for p in problems:
        log.warning(p)
    app = app or App(cfg)
    an = Analyses(app)
    mcp = MCPServer("prokas", instructions=INSTRUCTIONS, version="1.0.0")

    def audited(fn):
        name = fn.__name__

        @functools.wraps(fn)
        def wrapper(**kwargs) -> dict[str, Any]:
            t0 = time.monotonic()
            status, err, result = "ok", None, None
            try:
                result = fn(**kwargs)
                status = result.get("status", "ok") if isinstance(result, dict) else "ok"
                return result
            except BlockedWindowError as e:
                status, err = "blocked", str(e)
                result = {"status": "blocked", "message": str(e), "can_force": e.can_force,
                          "next_allowed": e.next_allowed.isoformat() if e.next_allowed else None}
                return result
            except (UserError, PolicyError, StagingError, ReadError) as e:
                status, err = "rejected", str(e)
                result = {"status": "error", "message": str(e)}
                return result
            except Exception as e:  # keep the server alive; show something usable
                log.exception("Tool %s failed", name)
                status, err = "error", f"{type(e).__name__}: {e}"
                result = {"status": "error", "message": f"Interner Fehler in {name}: {type(e).__name__}: {e}. "
                                                        f"Details im Log ({cfg.log_dir})."}
                return result
            finally:
                r = result if isinstance(result, dict) else {}
                try:
                    app.audit.log(name, kwargs, status=status, mode=r.get("mode"), files=r.get("files"),
                                  row_count=r.get("row_count"), redactions=r.get("redactions"),
                                  duration_ms=int((time.monotonic() - t0) * 1000), error=err,
                                  job_id=r.get("job_id"))
                except OSError:
                    log.exception("Audit log write failed")
                # File paths are for the audit log; the model gets file names in the result.
                r.pop("files", None)

        return wrapper

    def tool(annotations: ToolAnnotations, title: str):
        def deco(fn):
            mcp.add_tool(audited(fn), name=fn.__name__, title=title, description=fn.__doc__, annotations=annotations)
            return fn
        return deco

    # --- status & reports -------------------------------------------------

    @tool(RO, "Connector-Status")
    def connector_status() -> dict[str, Any]:
        """Health check: folders, whether report generation is allowed right now (blocked windows),
        CLI status, active/queued jobs, nightly set completeness, mapping validation. Use first when unsure."""
        return app.connector_status()

    @tool(GEN, "Liste abrufen")
    def run_report(
        z_code: Annotated[str, Field(description="Listennummer, z. B. Z0127 (auch '127' geht). Nur freigegebene Listen.")],
        date_from: Annotated[str | None, Field(description="Berichtszeitraum von (TT.MM.JJJJ, 'heute', 'gestern').")] = None,
        date_to: Annotated[str | None, Field(description="Berichtszeitraum bis (TT.MM.JJJJ).")] = None,
        period: Period = None,
        force_during_opening: Force = False,
        refresh: Annotated[bool, Field(description="Vorhandenen frischen Export ignorieren und neu erzeugen.")] = False,
    ) -> dict[str, Any]:
        """Get one Berichtswesen Z-list as clean, redacted rows. Order: fresh export file (mode=file) ->
        headless CLI (mode=cli) -> status=needs_computer_use with job_id, expected_file and a filled-in
        runbook. For needs_computer_use: run the runbook with computer use, then call ingest_export(job_id)."""
        return app.run_report(z_code, date_from=date_from, date_to=date_to, period=period,
                              force_during_opening=force_during_opening, refresh=refresh)

    @tool(RO, "Export einlesen")
    def ingest_export(
        job_id: Annotated[str, Field(description="job_id aus run_report bzw. dem Runbook.")],
        wait_sec: Annotated[float | None, Field(description="Max. Wartezeit auf die Datei in Sekunden (Standard 300).")] = None,
    ) -> dict[str, Any]:
        """After the runbook: wait (max 5 min) for the job's export file, parse it, remove personal data,
        return the rows and close the job. If the file is missing, says which runbook step to repeat."""
        return app.ingest_export(job_id, wait_sec=wait_sec)

    @tool(RO, "Auftragsstatus")
    def job_status(job_id: Annotated[str, Field(description="job_id")]) -> dict[str, Any]:
        """Status of one generation job. For a queued job whose turn has come, this starts it
        (and may return needs_computer_use with the runbook)."""
        return app.job_status(job_id)

    @tool(RO, "Aufträge")
    def list_jobs(include_finished: Annotated[bool, Field(description="Auch ältere, abgeschlossene Aufträge zeigen.")] = False) -> dict[str, Any]:
        """All generation jobs: the active one, the queue, and recent finished jobs."""
        return app.list_jobs(include_finished)

    @tool(GEN, "Auftrag abbrechen")
    def cancel_job(job_id: Annotated[str, Field(description="job_id")],
                   reason: Annotated[str, Field(description="Was war zu sehen / warum abgebrochen.")] = "") -> dict[str, Any]:
        """Cancel a job (e.g. an unknown dialog appeared during the runbook). Releases the lock so the
        next queued job can run. Changes nothing in PROKAS."""
        return app.cancel_job(job_id, reason)

    @tool(RO, "Aktualität der Exporte")
    def report_freshness() -> dict[str, Any]:
        """Latest export per allowed Z-list with age, and a warning if the nightly standard set
        (Berichtswesen Automatikfunktion) is missing or stale."""
        return app.report_freshness()

    @tool(RO, "Verfügbare Listen")
    def list_available_reports() -> dict[str, Any]:
        """The allowed Z-lists with title, description, whether a period is needed, mapped fields and
        whether the column mapping has been validated against a real export."""
        return app.list_available_reports()

    @tool(GEN, "Kommandozeile prüfen")
    def probe_cli(force: Annotated[bool, Field(description="Neu prüfen, auch wenn ein Ergebnis gespeichert ist.")] = False,
                  force_during_opening: Force = False) -> dict[str, Any]:
        """Look (read-only) for a way to run Berichtswesen lists headless and test candidates only with
        the harmless check list Z0327, a one-day period and output into jobs_dir. Result cached in
        cli_status.json; re-probes automatically after a PROKAS update."""
        return app.probe_cli(force=force, force_during_opening=force_during_opening)

    @tool(STAGE, "Spaltenzuordnung prüfen")
    def validate_mapping(
        z_code: Annotated[str | None, Field(description="Liste; leer = alle Listen mit Beispieldatei.")] = None,
        file: Annotated[str | None, Field(description="Dateiname in export_dir; leer = neueste Datei der Liste.")] = None,
        write_proposal: Annotated[bool, Field(description="Vorschlag (.yaml + .md) in die Outbox schreiben.")] = True,
    ) -> dict[str, Any]:
        """Check a list's column mapping against a real export: detected encoding/separator/header row,
        matched and unmatched columns, missing fields, values that failed German number/date parsing,
        3 redacted sample rows. Writes an updated mapping proposal into the outbox for a human to approve."""
        return app.validate_mapping(z_code, file, write_proposal)

    # --- analysis ---------------------------------------------------------

    @tool(GEN, "Artikel nachschlagen")
    def get_article(pzn: Annotated[str, Field(description="PZN (7 oder 8 Ziffern)")]) -> dict[str, Any]:
        """Everything the exported lists know about one article: stock, AMO, MB, EK/VK, margin, expiry,
        supplier, plus which lists it appears on (Ladenhüter, Spannenkiller, Verfall ...). Uses existing
        exports only; lists contain only conspicuous articles, so an article may not be found."""
        return an.get_article(pzn)

    @tool(GEN, "Spannen prüfen")
    def margin_check(
        threshold_pct: Annotated[float, Field(description="Artikel unter dieser Spanne (%) zeigen.")] = 20.0,
        period: Period = None,
        target_margin_pct: Annotated[float | None, Field(description="Zielspanne für VK-Vorschläge (%), Standard = threshold_pct.")] = None,
        refresh_optional: RefreshOpt = False,
        force_during_opening: Force = False,
    ) -> dict[str, Any]:
        """Low-margin articles from Z0125 (+ Z0585, Z0165 if available) with VK suggestions for a target
        margin. Suggestions only; price_file_items can be passed to stage_price_file after the owner agrees."""
        return an.margin_check(threshold_pct, period, target_margin_pct, refresh_optional, force_during_opening)

    @tool(GEN, "Verlorene Verkäufe")
    def lost_sales(period: Period = None, force_during_opening: Force = False) -> dict[str, Any]:
        """Demand (Z0015) and Neinverkäufe / lost sales (Z0016) aggregated by PZN, biggest first."""
        return an.lost_sales(period, force_during_opening)

    @tool(GEN, "Defektquote")
    def defect_rate(period: Period = None, force_during_opening: Force = False) -> dict[str, Any]:
        """Defektquote and Lieferfähigkeit from Z0245."""
        return an.defect_rate(period, force_during_opening)

    @tool(GEN, "Verfallrisiko")
    def expiry_risk(months: Annotated[int, Field(description="Verfall innerhalb so vieler Monate (Z0051 deckt 3 ab).")] = 3,
                    refresh_optional: RefreshOpt = False, force_during_opening: Force = False) -> dict[str, Any]:
        """Articles expiring soon with EK value at risk (Z0051), plus expiry articles without a date (Z0342)."""
        return an.expiry_risk(months, refresh_optional, force_during_opening)

    @tool(GEN, "Ladenhüter")
    def slow_movers(days: Annotated[int, Field(description="Tage ohne Verkauf (Liste enthält ab 60).")] = 60,
                    min_ek: Annotated[float, Field(description="Mindest-EK in € (Liste enthält ab 50).")] = 50.0,
                    force_during_opening: Force = False) -> dict[str, Any]:
        """Slow movers from Z0127 sorted by capital bound (Bestand × EK)."""
        return an.slow_movers(days, min_ek, force_during_opening)

    @tool(GEN, "Offene Retouren")
    def open_returns(force_during_opening: Force = False) -> dict[str, Any]:
        """Open returns to suppliers (Z0207), grouped by supplier with value and oldest date."""
        return an.open_returns(force_during_opening)

    @tool(GEN, "Offene Posten")
    def open_items(force_during_opening: Force = False) -> dict[str, Any]:
        """Open customer invoices (Z0705) as totals, ageing buckets and dunning levels.
        Amounts and dates only - no customers, names or invoice numbers."""
        return an.open_items(force_during_opening)

    @tool(GEN, "Stammdatenqualität")
    def inventory_data_quality(refresh_optional: RefreshOpt = False, force_during_opening: Force = False) -> dict[str, Any]:
        """Master-data problems for the inventory: no EK (Z0327), wrong VEK (Z0117), 8er articles with
        wrong EK maintenance (Z0134), stock value > 500 € (Z0322)."""
        return an.inventory_data_quality(refresh_optional, force_during_opening)

    @tool(GEN, "Kennzahlen")
    def kpi_snapshot(period: Period = None, refresh_optional: RefreshOpt = False,
                     force_during_opening: Force = False) -> dict[str, Any]:
        """Revenue, Rohertrag %, Lagerumschlag (Z0003, Z0165, Z0105) and defect rate (Z0245 if available)."""
        return an.kpi_snapshot(period, refresh_optional, force_during_opening)

    # --- staging ----------------------------------------------------------

    @tool(STAGE, "Preisdatei vorbereiten")
    def stage_price_file(
        items: Annotated[list[dict[str, Any]], Field(description="Liste von {pzn, price}; Preis in € brutto, > 0.")],
        valid_from: Annotated[str, Field(description="Gültig ab (TT.MM.JJJJ)")],
        valid_to: Annotated[str | None, Field(description="Gültig bis (TT.MM.JJJJ), leer = offen")] = None,
        label: Annotated[str, Field(description="Kurzer Name, z. B. 'Spannenkorrektur Oktober'")] = "",
    ) -> dict[str, Any]:
        """Write a price import file 'PZN;Preis' (comma decimals, Windows-1252, CRLF) plus a .md preview with
        old/new/difference into the outbox. Validates PZN check digits and prices. Never overwrites files.
        Nothing is changed in PROKAS - a human imports the file."""
        now = app.now()
        pzns = {str(i.get("pzn", "")) for i in items or [] if isinstance(i, dict)}
        old = _known_prices(app, pzns)
        content, md, rows = build_price_file(items, valid_from, valid_to or "", label, today=now.date(),
                                             old_prices=old, header=cfg.price_file_header)
        files = write_outbox_pair(cfg.outbox_dir, f"Preise_{label or 'Vorschlag'}", ".csv", content, md, now=now)
        return {"status": "ok", "message": f"Preisdatei mit {len(rows)} Artikeln in der Outbox abgelegt – bitte prüfen und importieren.",
                "outbox_files": [f.split("\\")[-1].split("/")[-1] for f in files], "outbox_dir": str(cfg.outbox_dir),
                "preview_markdown": md, "files": files, "row_count": len(rows)}

    @tool(STAGE, "Bestellliste vorbereiten")
    def stage_order_list(
        supplier: Annotated[str, Field(description="Lieferant, z. B. 'Gehe' oder 'Alliance'")],
        items: Annotated[list[dict[str, Any]], Field(description="Liste von {pzn, qty}")],
    ) -> dict[str, Any]:
        """Write an order proposal CSV 'PZN;Menge' plus .md preview into the outbox. Nothing is ordered."""
        names = {p: v.get("bezeichnung") for p, v in _known_prices(app, {str(i.get("pzn", "")) for i in items or []
                                                                         if isinstance(i, dict)}).items()}
        content, md, rows = build_order_list(supplier, items, names=names)
        files = write_outbox_pair(cfg.outbox_dir, f"Bestellung_{supplier}", ".csv", content, md, now=app.now())
        return {"status": "ok", "message": f"Bestellvorschlag ({len(rows)} Positionen) in der Outbox – es wurde nichts bestellt.",
                "outbox_files": [f.split("\\")[-1].split("/")[-1] for f in files], "outbox_dir": str(cfg.outbox_dir),
                "preview_markdown": md, "files": files, "row_count": len(rows)}

    # --- prompts ----------------------------------------------------------

    @mcp.prompt(name="prokas_export_runbook", title="PROKAS Export-Runbook",
                description="Checklist for exporting a Berichtswesen list with computer use.")
    def prokas_export_runbook(job_id: str = "") -> str:
        if job_id:
            try:
                return app.runbook_for(app.get_job(job_id))
            except UserError:
                pass
        return render_runbook(timeout_min=str(cfg.job_timeout_min))

    @mcp.prompt(name="pull_report", title="Liste holen", description="Pull a Z-list for a period and summarize it.")
    def pull_report(z_code: str, period: str = "") -> str:
        return (f"Hole die PROKAS-Liste {z_code} für den Zeitraum '{period or 'Standard'}' mit "
                f"run_report(z_code='{z_code}', period='{period}') und fasse sie auf Deutsch kurz zusammen "
                "(wichtigste Zahlen, Auffälligkeiten, max. 8 Zeilen). Kommt status=needs_computer_use zurück: "
                "Runbook per Bildschirmsteuerung genau ausführen, dann ingest_export(job_id) und danach zusammenfassen. "
                "Bei status=blocked dem Inhaber die Sperrzeit und den nächstmöglichen Zeitpunkt nennen.")

    @mcp.prompt(name="morning_check", title="Morgen-Check", description="Daily check: freshness, defects, expiry, lost sales.")
    def morning_check() -> str:
        return ("Morgen-Check für die easyApotheke Nedderfeld. Rufe nacheinander auf: report_freshness(), "
                "defect_rate(period='letzte 7 Tage'), expiry_risk(months=3), lost_sales(period='letzte 7 Tage'). "
                "Wenn ein Tool needs_computer_use liefert: Runbook ausführen, ingest_export(job_id), dann das Tool erneut. "
                "Antworte auf Deutsch, kompakt fürs iPhone: 1) Nachtset ok? 2) Defektquote 3) Verfall (Wert, Top 5) "
                "4) Top 5 verlorene Verkäufe 5) Was heute zu tun ist (max. 3 Punkte).")

    @mcp.prompt(name="margin_review", title="Spannen-Review", description="Margin check plus a staged price-file proposal.")
    def margin_review(threshold_pct: str = "20", target_margin_pct: str = "") -> str:
        return (f"Rufe margin_check(threshold_pct={threshold_pct}"
                + (f", target_margin_pct={target_margin_pct}" if target_margin_pct else "")
                + ") auf. Zeige die 15 Artikel mit der kleinsten Spanne (PZN, Name, EK, VK, Spanne, VK-Vorschlag). "
                "Frage den Inhaber, welche Vorschläge er übernehmen will. ERST NACH seiner Zustimmung "
                "stage_price_file mit genau diesen Artikeln (aus price_file_items) aufrufen, valid_from = morgen, "
                "label = 'Spannenkorrektur'. Hinweis: Es wird nur eine Datei in der Outbox abgelegt, PROKAS bleibt unverändert.")

    return mcp, app


def _known_prices(app: App, pzns: set[str]) -> dict[str, dict]:
    """Latest VK/name for PZNs from existing exports (for previews)."""
    from .analysis import Analyses
    from .german import normalize_pzn

    wanted = {normalize_pzn(p) for p in pzns if normalize_pzn(p)}
    out: dict[str, dict] = {}
    if not wanted:
        return out
    an = Analyses(app)
    for z in app.cfg.allowed_z_codes:
        m = app.mappings.get(z)
        if not m or m.key != "pzn":
            continue
        o = an._existing(z, None)
        if not o:
            continue
        for r in o.parsed.records:
            p = r.get("pzn")
            if p in wanted:
                e = out.setdefault(p, {"source": z})
                if e.get("vk") is None and isinstance(r.get("vk"), (int, float)):
                    e["vk"], e["source"] = r["vk"], z
                if not e.get("bezeichnung") and r.get("bezeichnung"):
                    e["bezeichnung"] = r["bezeichnung"]
    return out


def startup_probe(app: App) -> None:
    """First start: probe the CLI in the background (it defers its test outside allowed windows)."""
    st = app.cli_status.load()
    if st.get("status", "unknown") != "unknown":
        return

    def run():
        try:
            app.probe_cli(force=True)
            app.audit.log("probe_cli", {"trigger": "startup"}, status="ok", mode="cli")
        except Exception:
            log.exception("Startup CLI probe failed")

    threading.Thread(target=run, name="prokas-cli-probe", daemon=True).start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mcp, app = build_server()
    try:
        fh = logging.FileHandler(app.cfg.log_dir / "server.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    except OSError:
        pass
    startup_probe(app)
    if active_block(app.now(), app.cfg, False):
        log.info("Report generation currently blocked; exports are still readable.")
    mcp.run("stdio")
