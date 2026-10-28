"""Analysis tools on parsed, redacted list data (Part D).

Every tool names the lists it needs. Missing or stale *required* lists are requested
through App.obtain (file -> CLI -> GUI); if one needs computer use, the tool returns
that status unchanged so the agent can run the runbook and call the tool again.
Optional lists are used when present and only generated with refresh_optional=true.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from .app import App, Obtained, UserError
from .german import format_de_number, validate_pzn
from .guards import PolicyError
from .parser import json_value
from .redaction import RedactionReport
from .staging import round_up_price

RX_MARKERS = {"rx", "rp", "ja", "j", "x", "verschreibungspflichtig", "rezeptpflichtig", "btm"}

# What it means for an article to appear on a list (for get_article).
LIST_MEANING = {
    "Z0015": "wurde im Zeitraum nachgefragt",
    "Z0016": "Neinverkauf im Zeitraum",
    "Z0051": "verfällt in den nächsten 3 Monaten",
    "Z0102": "Lagerwertverlust durch EK-Senkung",
    "Z0117": "Verrechnungs-EK passt nicht (Inventur-Prüfliste)",
    "Z0125": "Spannenkiller in der Freiwahl (< 20 %)",
    "Z0127": "Ladenhüter (kein Verkauf seit > 60 Tagen, EK > 50 €)",
    "Z0134": "8er-Artikel mit falscher EK-Pflege",
    "Z0207": "offene Retoure",
    "Z0317": "Preis im Zeitraum geändert",
    "Z0322": "Lagerwert > 500 €",
    "Z0327": "kein Listen-/Verrechnungs-EK",
    "Z0342": "Verfallartikel ohne Verfalldatum",
    "Z0585": "selbst kalkulierter Artikel (Spannenkontrolle)",
    "Z0713": "BestRX: roter Lagerartikel",
    "Z0714": "BestRX: künftig roter Lagerartikel",
}


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _clean(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({k: (None if (isinstance(v, float) and v != v) else json_value(v)) for k, v in r.items()
                    if not (v is None or (isinstance(v, float) and v != v))})
    return out


def _eur(v: float, decimals: int = 2) -> str:
    return f"{format_de_number(v, decimals)} €"


def _pct(v: float, decimals: int = 1) -> str:
    return f"{format_de_number(v, decimals)} %"


def _is_rx(v: Any) -> bool:
    return str(v or "").strip().lower() in RX_MARKERS


class Analyses:
    def __init__(self, app: App):
        self.app = app

    # --- gathering ----------------------------------------------------------

    def gather(self, tool: str, required: list[str], optional: list[str] = (), *, period: str | None = None,
               refresh_optional: bool = False, force_during_opening: bool = False,
               period_lists: set[str] | None = None) -> tuple[dict[str, Obtained], dict | None, list[str]]:
        got: dict[str, Obtained] = {}
        pending: list[dict] = []
        notes: list[str] = []
        for z in list(required) + list(optional):
            is_req = z in required
            p = period if (period_lists is None or z in period_lists) else None
            if not is_req and not refresh_optional:
                found = self._existing(z, p)
                if found:
                    got[z] = found
                else:
                    notes.append(f"{z} ({self._title(z)}) nicht vorhanden – optional, mit refresh_optional=true erzeugen.")
                continue
            try:
                o = self.app.obtain(z, period=p, force_during_opening=force_during_opening)
            except PolicyError as e:
                raise UserError(str(e)) from e
            if o.pending:
                pending.append({"z_code": z, **o.pending})
                if not is_req:
                    notes.append(f"{z} wird gerade beschafft (optional).")
            else:
                got[z] = o
        missing_req = [p for p in pending if p["z_code"] in required]
        if missing_req:
            return got, self._pending_response(tool, pending), notes
        return got, None, notes

    def _existing(self, z: str, period: str | None) -> Obtained | None:
        """Latest export without triggering generation (stale allowed, flagged)."""
        from .guards import resolve_period

        if z not in self.app.cfg.allowed_z_codes or z not in self.app.mappings:
            return None
        m = self.app.mappings[z]
        try:
            p = resolve_period(self.app.now().date(), self.app.cfg, period=period, needs_period=m.needs_period)
        except PolicyError:
            return None
        found, _ = self.app.find_export(z, p, m, None)
        return found

    def _title(self, z: str) -> str:
        m = self.app.mappings.get(z)
        return m.title if m else z

    def _pending_response(self, tool: str, pending: list[dict]) -> dict:
        first_gui = next((p for p in pending if p.get("status") == "needs_computer_use"), None)
        statuses = {p.get("status") for p in pending}
        status = ("needs_computer_use" if first_gui else "queued" if "queued" in statuses
                  else "blocked" if "blocked" in statuses else next(iter(statuses)))
        lists = ", ".join(p["z_code"] for p in pending)
        out: dict[str, Any] = {
            "status": status,
            "message": (f"Für {tool} fehlen aktuelle Daten ({lists}). "
                        + (f"Zuerst {first_gui['z_code']} per Bildschirmsteuerung exportieren (Runbook), dann "
                           f"ingest_export('{first_gui['job_id']}') und danach {tool} erneut aufrufen."
                           if first_gui else
                           " ".join(p.get("message", "") for p in pending))),
            "pending": [{k: v for k, v in p.items() if k in ("z_code", "status", "job_id", "message", "expected_file",
                                                               "next_allowed", "can_force")} for p in pending],
            "then": f"Nach dem Export {tool} erneut aufrufen.",
        }
        if first_gui:
            out.update(job_id=first_gui["job_id"], expected_file=first_gui["expected_file"],
                       runbook=first_gui["runbook"], mode="gui")
        return out

    def _result(self, got: dict[str, Obtained], notes: list[str], **payload) -> dict:
        now = self.app.now()
        red = RedactionReport()
        sources, files, warnings, rows = [], [], list(notes), 0
        for z, o in got.items():
            pr = o.parsed
            red.merge(pr.redactions)
            files.append(str(pr.path))
            rows += pr.row_count
            age = round((now - dt.datetime.fromtimestamp(pr.path.stat().st_mtime, self.app.tz)).total_seconds() / 3600, 1)
            period = pr.period or (o.export.period if o.export else None)
            sources.append({"z_code": z, "title": self._title(z), "file": pr.path.name, "age_hours": age,
                            "period": f"{period[0]} – {period[1]}" if period else None, "mode": o.mode,
                            "rows": pr.row_count, "mapping_validated": pr.mapping_validated})
            if o.stale or age >= self.app.cfg.stale_after_hours:
                warnings.append(f"{z}: Daten sind {age} h alt.")
            if o.note:
                warnings.append(o.note)
            if not pr.mapping_validated:
                warnings.append(f"{z}: Spaltenzuordnung noch nicht an echter Datei bestätigt.")
        modes = {s["mode"] for s in sources}
        out = {"status": "ok", **payload, "sources": sources, "warnings": warnings,
               "files": files, "row_count": rows, "redactions": red.as_dict(),
               "mode": modes.pop() if len(modes) == 1 else ("mixed" if modes else None)}
        return out

    def _df(self, o: Obtained) -> pd.DataFrame:
        return o.parsed.dataframe()

    def _limit(self) -> int:
        return min(self.app.cfg.max_rows_returned, 200)

    # --- get_article --------------------------------------------------------

    def get_article(self, pzn: str) -> dict:
        p, err = validate_pzn(pzn)
        if err:
            raise UserError(err)
        got: dict[str, Obtained] = {}
        hits: dict[str, list[dict]] = {}
        for z in self.app.cfg.allowed_z_codes:
            m = self.app.mappings.get(z)
            if not m or m.key != "pzn":
                continue
            o = self._existing(z, None)
            if not o:
                continue
            rows = [r for r in o.parsed.records if r.get("pzn") == p]
            if rows:
                got[z] = o
                hits[z] = rows
        if not hits:
            return {"status": "not_found", "pzn": p,
                    "message": (f"PZN {p} steht in keiner vorhandenen Liste. Die Listen enthalten nur auffällige Artikel "
                                f"(z. B. Ladenhüter, Spannenkiller, Verfall). Für Stammdaten in PROKAS nachsehen."),
                    "searched_lists": [z for z in self.app.cfg.allowed_z_codes if self._existing(z, None)]}
        # Newest source wins per field.
        order = sorted(got, key=lambda z: got[z].parsed.path.stat().st_mtime, reverse=True)
        article: dict[str, Any] = {"pzn": p}
        field_sources: dict[str, str] = {}
        for z in order:
            row = hits[z][0]
            for k, v in row.items():
                if k in ("pzn", "datum", "menge", "art", "grund") or v is None:
                    continue
                if k not in article:
                    article[k] = json_value(v)
                    field_sources[k] = z
        ek, vk = _num(article.get("ek")), _num(article.get("vk"))
        if ek and vk and "spanne" not in article:
            vat = _num(article.get("mwst")) or self.app.cfg.default_vat_pct
            net = vk / (1 + vat / 100)
            article["spanne_berechnet"] = round((net - ek) / net * 100, 1) if net else None
        flags = []
        for z in order:
            text = LIST_MEANING.get(z)
            if z in ("Z0015", "Z0016"):
                total = sum(_num(r.get("menge")) or 1 for r in hits[z])
                text = f"{text}: {len(hits[z])}× ({total:g} Packungen)"
            flags.append({"z_code": z, "meaning": text or self._title(z)})
        name = article.get("bezeichnung", "")
        return self._result(got, [], pzn=p, message=f"PZN {p} {name}: in {len(hits)} Listen gefunden.",
                            article=article, field_sources=field_sources, appears_in=flags)

    # --- margin_check -------------------------------------------------------

    def margin_check(self, threshold_pct: float = 20.0, period: str | None = None,
                     target_margin_pct: float | None = None, refresh_optional: bool = False,
                     force_during_opening: bool = False) -> dict:
        if not 0 < threshold_pct < 100:
            raise UserError("threshold_pct muss zwischen 0 und 100 liegen.")
        target = target_margin_pct if target_margin_pct is not None else threshold_pct
        if not 0 < target < 95:
            raise UserError("target_margin_pct muss zwischen 0 und 95 liegen.")
        got, pending, notes = self.gather("margin_check", ["Z0125"], ["Z0585", "Z0165"], period=period,
                                          refresh_optional=refresh_optional, force_during_opening=force_during_opening,
                                          period_lists={"Z0585", "Z0165"})
        if pending:
            return pending
        frames = []
        for z in ("Z0125", "Z0585"):
            if z in got:
                df = self._df(got[z])
                df["quelle"] = z
                frames.append(df)
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if df.empty:
            return self._result(got, notes, message="Keine Artikel in den Spannenlisten.", items=[], price_file_items=[])
        df = df.drop_duplicates(subset=["pzn"], keep="first")
        rx_count = 0
        if "rx" in df:
            rx = df["rx"].map(_is_rx)
            rx_count = int(rx.sum())
            df = df[~rx]
        items, price_items, no_ek = [], [], 0
        step = self.app.cfg.price_rounding_step
        for r in df.to_dict("records"):
            ek, vk = _num(r.get("ek")), _num(r.get("vk"))
            vat = _num(r.get("mwst")) or self.app.cfg.default_vat_pct
            spanne = _num(r.get("spanne"))
            if spanne is None and ek and vk:
                net = vk / (1 + vat / 100)
                spanne = (net - ek) / net * 100 if net else None
            if spanne is None or spanne >= threshold_pct:
                continue
            item = {"pzn": r.get("pzn"), "bezeichnung": r.get("bezeichnung"), "packung": r.get("packung"),
                    "ek": ek, "vk": vk, "mwst": vat, "spanne_pct": round(spanne, 1), "quelle": r["quelle"]}
            if _num(r.get("amo")) is not None:
                item["amo"] = _num(r.get("amo"))
            if ek and ek > 0:
                suggested = round_up_price(ek / (1 - target / 100) * (1 + vat / 100), step)
                if vk is None or suggested > vk:
                    item["vk_vorschlag"] = suggested
                    item["vk_differenz"] = round(suggested - vk, 2) if vk else None
                    price_items.append({"pzn": r.get("pzn"), "price": suggested})
            else:
                no_ek += 1
            items.append(item)
        items.sort(key=lambda x: x["spanne_pct"])
        if rx_count:
            notes.append(f"{rx_count} verschreibungspflichtige Artikel ausgelassen (Festpreise).")
        if no_ek:
            notes.append(f"{no_ek} Artikel ohne EK – kein VK-Vorschlag möglich.")
        overview = {}
        if "Z0165" in got:
            d = self._df(got["Z0165"])
            if {"umsatz", "rohertrag"} <= set(d.columns) and d["umsatz"].sum():
                overview = {"umsatz": round(float(d["umsatz"].sum()), 2), "rohertrag": round(float(d["rohertrag"].sum()), 2),
                            "rohertrag_pct": round(float(d["rohertrag"].sum() / d["umsatz"].sum() * 100), 1)}
        lim = self._limit()
        return self._result(
            got, notes,
            message=(f"{len(items)} Artikel unter {threshold_pct:g} % Spanne, {len(price_items)} VK-Vorschläge "
                     f"für {target:g} % Zielspanne (nur Vorschläge, nichts geändert)."),
            threshold_pct=threshold_pct, target_margin_pct=target, count=len(items), items=_clean(items[:lim]),
            truncated=len(items) > lim, price_file_items=price_items[:lim], rohertrag_overview=overview,
            formula="Spanne = (VK/(1+MwSt) − EK) ÷ (VK/(1+MwSt)); VK-Vorschlag = EK ÷ (1 − Ziel) × (1+MwSt), "
                    f"aufgerundet auf {step:g} €",
        )

    # --- lost_sales ----------------------------------------------------------

    def lost_sales(self, period: str | None = None, force_during_opening: bool = False) -> dict:
        got, pending, notes = self.gather("lost_sales", ["Z0015", "Z0016"], period=period,
                                          force_during_opening=force_during_opening)
        if pending:
            return pending
        agg: dict[str, dict] = {}
        for z, key in (("Z0015", "nachfragen"), ("Z0016", "neinverkauf")):
            for r in got[z].parsed.records:
                pzn = r.get("pzn")
                a = agg.setdefault(pzn, {"pzn": pzn, "bezeichnung": None, "packung": None, "anbieter": None,
                                         "nachfragen_anzahl": 0, "nachfragen_menge": 0.0,
                                         "neinverkauf_anzahl": 0, "neinverkauf_menge": 0.0})
                for f in ("bezeichnung", "packung", "anbieter", "bestand"):
                    if r.get(f) is not None and a.get(f) is None:
                        a[f] = r[f]
                a[f"{key}_anzahl"] += 1
                a[f"{key}_menge"] += _num(r.get("menge")) or 1
        items = sorted(agg.values(), key=lambda a: (a["neinverkauf_menge"], a["nachfragen_menge"]), reverse=True)
        tot_n = sum(a["nachfragen_menge"] for a in items)
        tot_nv = sum(a["neinverkauf_menge"] for a in items)
        lim = self._limit()
        return self._result(
            got, notes,
            message=(f"{len(items)} Artikel nachgefragt: {tot_n:g} Packungen Nachfrage, davon {tot_nv:g} Neinverkäufe "
                     f"(verlorene Verkäufe)."),
            totals={"artikel": len(items), "nachfragen_packungen": tot_n, "neinverkauf_packungen": tot_nv},
            items=_clean(items[:lim]), truncated=len(items) > lim,
            hint="Nachfragen (Z0015) enthalten Kundenbestellungen und Neinverkäufe; Neinverkäufe (Z0016) = unbefriedigte Nachfrage.",
        )

    # --- defect_rate ----------------------------------------------------------

    def defect_rate(self, period: str | None = None, force_during_opening: bool = False) -> dict:
        got, pending, notes = self.gather("defect_rate", ["Z0245"], period=period, force_during_opening=force_during_opening)
        if pending:
            return pending
        df = self._df(got["Z0245"])
        rate = lief = None
        how = None
        if {"nachfragen", "abgaben"} <= set(df.columns) and df["abgaben"].sum():
            rate = float(df["nachfragen"].sum() / df["abgaben"].sum() * 100)
            how = "Summe Nachfragen ÷ Summe Abgaben"
        elif "defektquote" in df and df["defektquote"].notna().any():
            rate = float(df["defektquote"].dropna().iloc[-1]) if len(df) == 1 else float(df["defektquote"].dropna().mean())
            how = "Defektquote laut Liste" + (" (Mittelwert der Zeilen)" if len(df) > 1 else "")
        if "lieferfaehigkeit" in df and df["lieferfaehigkeit"].notna().any():
            lief = float(df["lieferfaehigkeit"].dropna().mean())
        msg = f"Defektquote {_pct(rate)}" if rate is not None else "Defektquote nicht berechenbar"
        if lief is not None:
            msg += f", Lieferfähigkeit {_pct(lief)}"
        return self._result(got, notes, message=msg + ".", defektquote_pct=round(rate, 2) if rate is not None else None,
                            lieferfaehigkeit_pct=round(lief, 2) if lief is not None else None, method=how,
                            rows=_clean(df.to_dict("records")[: self._limit()]))

    # --- expiry_risk ----------------------------------------------------------

    def expiry_risk(self, months: int = 3, refresh_optional: bool = False, force_during_opening: bool = False) -> dict:
        if not 1 <= months <= 24:
            raise UserError("months muss zwischen 1 und 24 liegen.")
        got, pending, notes = self.gather("expiry_risk", ["Z0051"], ["Z0342"], refresh_optional=refresh_optional,
                                          force_during_opening=force_during_opening)
        if pending:
            return pending
        if months > 3:
            notes.append("Z0051 enthält nur Artikel, die in den nächsten 3 Monaten verfallen – längere Zeiträume sind unvollständig.")
        today = self.app.now().date()
        limit_date = (pd.Timestamp(today) + pd.DateOffset(months=months)).date()
        items, expired = [], 0
        for r in got["Z0051"].parsed.records:
            v = r.get("verfall")
            if v is None or v > limit_date:
                continue
            qty = _num(r.get("menge_verfall")) or _num(r.get("bestand"))
            ek = _num(r.get("ek"))
            value = _num(r.get("lagerwert")) or (qty * ek if qty is not None and ek is not None else None)
            if v < today:
                expired += 1
            items.append({"pzn": r.get("pzn"), "bezeichnung": r.get("bezeichnung"), "packung": r.get("packung"),
                          "verfall": v, "tage_bis_verfall": (v - today).days, "menge": qty, "ek": ek,
                          "wert_ek": round(value, 2) if value is not None else None, "lagerort": r.get("lagerort")})
        items.sort(key=lambda x: x["verfall"])
        total = round(sum(i["wert_ek"] or 0 for i in items), 2)
        no_date = []
        if "Z0342" in got:
            no_date = [{"pzn": r.get("pzn"), "bezeichnung": r.get("bezeichnung"), "bestand": r.get("bestand"),
                        "lagerort": r.get("lagerort")} for r in got["Z0342"].parsed.records]
        lim = self._limit()
        msg = f"{len(items)} Artikel verfallen bis {limit_date:%d.%m.%Y} (EK-Wert ca. {_eur(total)})"
        if expired:
            msg += f", davon {expired} bereits verfallen"
        if "Z0342" in got:
            msg += f"; {len(no_date)} Verfallartikel ohne Datum"
        return self._result(got, notes, message=msg + ".", months=months, until=limit_date.isoformat(),
                            value_at_risk_ek=total, already_expired=expired, items=_clean(items[:lim]),
                            truncated=len(items) > lim, without_date=_clean(no_date[:lim]))

    # --- slow_movers ----------------------------------------------------------

    def slow_movers(self, days: int = 60, min_ek: float = 50.0, force_during_opening: bool = False) -> dict:
        got, pending, notes = self.gather("slow_movers", ["Z0127"], force_during_opening=force_during_opening)
        if pending:
            return pending
        if days < 60 or min_ek < 50:
            notes.append("Z0127 enthält nur Artikel ohne Verkauf seit > 60 Tagen und EK > 50 € – kleinere Werte liefern nicht mehr Artikel.")
        today = self.app.now().date()
        cutoff = today - dt.timedelta(days=days)
        items = []
        for r in got["Z0127"].parsed.records:
            last = r.get("letzter_verkauf")
            ek = _num(r.get("ek"))
            if last is not None and last > cutoff:
                continue
            if ek is not None and ek < min_ek:
                continue
            qty = _num(r.get("bestand"))
            value = _num(r.get("lagerwert")) or (qty * ek if qty is not None and ek is not None else None)
            items.append({"pzn": r.get("pzn"), "bezeichnung": r.get("bezeichnung"), "packung": r.get("packung"),
                          "anbieter": r.get("anbieter"), "bestand": qty, "ek": ek, "lagerwert": round(value, 2) if value else value,
                          "letzter_verkauf": last, "tage_ohne_verkauf": (today - last).days if last else None,
                          "lagerort": r.get("lagerort")})
        items.sort(key=lambda x: x["lagerwert"] or 0, reverse=True)
        total = round(sum(i["lagerwert"] or 0 for i in items), 2)
        lim = self._limit()
        return self._result(got, notes, message=f"{len(items)} Ladenhüter, gebundenes Kapital ca. {_eur(total)}.",
                            days=days, min_ek=min_ek, capital_bound=total, items=_clean(items[:lim]), truncated=len(items) > lim)

    # --- open_returns -----------------------------------------------------------

    def open_returns(self, force_during_opening: bool = False) -> dict:
        got, pending, notes = self.gather("open_returns", ["Z0207"], force_during_opening=force_during_opening)
        if pending:
            return pending
        df = self._df(got["Z0207"])
        by_supplier = []
        if not df.empty:
            key = "lieferant" if "lieferant" in df and df["lieferant"].notna().any() else None
            groups = df.groupby(df[key].fillna("(ohne Lieferant)")) if key else [("(alle)", df)]
            for name, g in groups:
                entry = {"lieferant": name, "positionen": int(len(g))}
                if "wert" in g:
                    entry["wert"] = round(float(g["wert"].fillna(0).sum()), 2)
                if "retoure_datum" in g and g["retoure_datum"].notna().any():
                    oldest = min(d for d in g["retoure_datum"] if d is not None)
                    entry["aelteste_retoure"] = oldest.isoformat()
                    entry["tage_offen_max"] = (self.app.now().date() - oldest).days
                by_supplier.append(entry)
        total = round(float(df["wert"].fillna(0).sum()), 2) if "wert" in df else None
        lim = self._limit()
        return self._result(got, notes, message=f"{len(df)} offene Retourenpositionen" + (f", Wert {_eur(total)}" if total is not None else "") + ".",
                            by_supplier=by_supplier, total_value=total, items=_clean(df.to_dict("records")[:lim]),
                            truncated=len(df) > lim)

    # --- open_items -----------------------------------------------------------

    def open_items(self, force_during_opening: bool = False) -> dict:
        got, pending, notes = self.gather("open_items", ["Z0705"], force_during_opening=force_during_opening)
        if pending:
            return pending
        today = self.app.now().date()
        buckets = {"nicht fällig": [0, 0.0], "1–30 Tage": [0, 0.0], "31–60 Tage": [0, 0.0], "61–90 Tage": [0, 0.0],
                   "> 90 Tage": [0, 0.0], "ohne Datum": [0, 0.0]}
        by_stage: dict[str, list] = {}
        oldest = []
        total = 0.0
        for r in got["Z0705"].parsed.records:
            amount = _num(r.get("offen")) or 0.0
            total += amount
            ref = r.get("faellig") or r.get("rechnungsdatum")
            if ref is None:
                b = "ohne Datum"
            else:
                over = (today - ref).days
                b = ("nicht fällig" if over <= 0 else "1–30 Tage" if over <= 30 else "31–60 Tage" if over <= 60
                     else "61–90 Tage" if over <= 90 else "> 90 Tage")
            buckets[b][0] += 1
            buckets[b][1] += amount
            st = str(r.get("mahnstufe") if r.get("mahnstufe") is not None else "keine")
            by_stage.setdefault(st, [0, 0.0])
            by_stage[st][0] += 1
            by_stage[st][1] += amount
            oldest.append({"rechnungsdatum": r.get("rechnungsdatum"), "faellig": r.get("faellig"),
                           "offen": amount, "mahnstufe": r.get("mahnstufe")})
        oldest.sort(key=lambda x: x["faellig"] or x["rechnungsdatum"] or dt.date.max)
        n = len(got["Z0705"].parsed.records)
        return self._result(
            got, notes,
            message=f"{n} offene Posten, zusammen {_eur(total)}.",
            count=n, total_open=round(total, 2),
            aging=[{"ueberfaellig": k, "anzahl": v[0], "betrag": round(v[1], 2)} for k, v in buckets.items() if v[0]],
            by_dunning_level=[{"mahnstufe": k, "anzahl": v[0], "betrag": round(v[1], 2)} for k, v in sorted(by_stage.items())],
            oldest=_clean(oldest[:20]),
            privacy="Nur Beträge und Daten – Kunden, Namen und Rechnungsnummern werden nicht übertragen.",
        )

    # --- inventory_data_quality -------------------------------------------------

    def inventory_data_quality(self, refresh_optional: bool = False, force_during_opening: bool = False) -> dict:
        got, pending, notes = self.gather("inventory_data_quality", ["Z0327"], ["Z0117", "Z0134", "Z0322"],
                                          refresh_optional=refresh_optional, force_during_opening=force_during_opening)
        if pending:
            return pending
        checks = []
        lim = min(50, self._limit())
        for z in ("Z0327", "Z0117", "Z0134", "Z0322"):
            if z not in got:
                checks.append({"z_code": z, "title": self._title(z), "available": False})
                continue
            df = self._df(got[z])
            entry = {"z_code": z, "title": self._title(z), "available": True, "count": int(len(df))}
            if "lagerwert" in df:
                entry["lagerwert_summe"] = round(float(df["lagerwert"].fillna(0).sum()), 2)
            if z == "Z0327" and "bestand" in df:
                entry["mit_bestand"] = int((df["bestand"].fillna(0) > 0).sum())
            if z == "Z0117" and {"vek", "vek_bereinigt"} <= set(df.columns):
                entry["vek_differenz_summe"] = round(float((df["vek"] - df["vek_bereinigt"]).abs().fillna(0).sum()), 2)
            sort_col = "lagerwert" if "lagerwert" in df else "bestand" if "bestand" in df else None
            top = df.sort_values(sort_col, ascending=False) if sort_col else df
            entry["items"] = _clean(top.to_dict("records")[:lim])
            checks.append(entry)
        issues = sum(c.get("count", 0) for c in checks if c["z_code"] != "Z0322")
        return self._result(got, notes, message=f"{issues} Artikel mit Stammdaten-Problemen (EK fehlt/falsch).",
                            checks=checks)

    # --- kpi_snapshot ---------------------------------------------------------

    def kpi_snapshot(self, period: str | None = None, refresh_optional: bool = False,
                     force_during_opening: bool = False) -> dict:
        got, pending, notes = self.gather("kpi_snapshot", ["Z0003", "Z0165", "Z0105"], ["Z0245"], period=period,
                                          refresh_optional=refresh_optional, force_during_opening=force_during_opening)
        if pending:
            return pending
        kpi: dict[str, Any] = {}
        d3 = self._df(got["Z0003"])
        if "umsatz" in d3:
            kpi["umsatz_netto"] = round(float(d3["umsatz"].fillna(0).sum()), 2)
        if "umsatz_brutto" in d3 and d3["umsatz_brutto"].notna().any():
            kpi["umsatz_brutto"] = round(float(d3["umsatz_brutto"].fillna(0).sum()), 2)
        d165 = self._df(got["Z0165"])
        src = d165 if {"umsatz", "rohertrag"} <= set(d165.columns) else d3
        if {"umsatz", "rohertrag"} <= set(src.columns) and src["umsatz"].sum():
            kpi["rohertrag"] = round(float(src["rohertrag"].sum()), 2)
            kpi["rohertrag_pct"] = round(float(src["rohertrag"].sum() / src["umsatz"].sum() * 100), 1)
        if "umsatz_vj" in d165 and d165["umsatz_vj"].notna().any() and d165["umsatz_vj"].sum():
            kpi["umsatz_vs_vorjahr_pct"] = round(float((d165["umsatz"].sum() / d165["umsatz_vj"].sum() - 1) * 100), 1)
        d105 = self._df(got["Z0105"])
        if {"abgaben_ek", "lagerwert"} <= set(d105.columns) and d105["lagerwert"].sum():
            kpi["lagerumschlag"] = round(float(d105["abgaben_ek"].sum() / d105["lagerwert"].sum()), 2)
        elif "lagerumschlag" in d105 and d105["lagerumschlag"].notna().any():
            if "lagerwert" in d105 and d105["lagerwert"].sum():
                w = d105["lagerwert"].fillna(0)
                kpi["lagerumschlag"] = round(float((d105["lagerumschlag"].fillna(0) * w).sum() / w.sum()), 2)
            else:
                kpi["lagerumschlag"] = round(float(d105["lagerumschlag"].mean()), 2)
        if "lagerwert" in d105:
            kpi["lagerwert"] = round(float(d105["lagerwert"].fillna(0).sum()), 2)
        if "Z0245" in got:
            d = self._df(got["Z0245"])
            if {"nachfragen", "abgaben"} <= set(d.columns) and d["abgaben"].sum():
                kpi["defektquote_pct"] = round(float(d["nachfragen"].sum() / d["abgaben"].sum() * 100), 2)
            elif "defektquote" in d and d["defektquote"].notna().any():
                kpi["defektquote_pct"] = round(float(d["defektquote"].dropna().mean()), 2)
        notes.append("Summen aus den Einzelzeilen der Listen (Summenzeilen werden entfernt) – bei Abweichungen mit der Gesamtzeile in PROKAS abgleichen.")
        parts = []
        if "umsatz_netto" in kpi:
            parts.append(f"Umsatz netto {_eur(kpi['umsatz_netto'], 0)}")
        if "rohertrag_pct" in kpi:
            parts.append(f"Rohertrag {_pct(kpi['rohertrag_pct'])}")
        if "lagerumschlag" in kpi:
            parts.append(f"Lagerumschlag {format_de_number(kpi['lagerumschlag'], 1)}")
        if "defektquote_pct" in kpi:
            parts.append(f"Defektquote {_pct(kpi['defektquote_pct'])}")
        msg = ", ".join(parts) or "Keine Kennzahlen berechenbar"
        return self._result(got, notes, message=msg + ".", kpi=kpi,
                            by_status=_clean(d165.to_dict("records")[:50]),
                            by_revenue_group=_clean(d3.to_dict("records")[:50]),
                            by_storage_location=_clean(d105.to_dict("records")[:50]))
