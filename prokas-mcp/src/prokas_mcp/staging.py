"""Staging: import files for a human to check and import. Only ever written to outbox_dir."""

from __future__ import annotations

import datetime as dt
import math
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .german import format_de_date, format_de_number, parse_number, validate_pzn
from .guards import PolicyError, parse_day


class StagingError(PolicyError):
    pass


def _slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß_-]+", "_", str(text or "").strip()).strip("_")
    return (s or "ohne_Titel")[:maxlen]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def write_outbox_pair(outbox: Path, stem: str, ext: str, content: str, preview_md: str, *,
                      encoding: str = "cp1252", newline: str = "\r\n", now: dt.datetime | None = None) -> list[str]:
    """Write <stem>_<ts><ext> plus <stem>_<ts>.md. Never overwrites: adds _2, _3 ... if needed."""
    outbox = Path(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    ts = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = f"{_slug(stem, 80)}_{ts}"
    for n in range(1, 100):
        name = base if n == 1 else f"{base}_{n}"
        data_path, md_path = outbox / f"{name}{ext}", outbox / f"{name}.md"
        if not (_inside(data_path, outbox) and _inside(md_path, outbox)):
            raise StagingError("Dateien dürfen nur im Outbox-Ordner angelegt werden.")
        if data_path.exists() or md_path.exists():
            continue
        try:
            with open(data_path, "x", encoding=encoding, newline="") as fh:
                fh.write(content.replace("\r\n", "\n").replace("\n", newline))
        except FileExistsError:
            continue
        with open(md_path, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(preview_md)
        return [str(data_path), str(md_path)]
    raise StagingError("Kein freier Dateiname im Outbox-Ordner gefunden.")


def _price(value: Any) -> Decimal:
    n = parse_number(value)
    if n is None or math.isnan(n) or math.isinf(n):
        raise StagingError(f"Preis {value!r} ist keine Zahl.")
    d = Decimal(str(n)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d <= 0:
        raise StagingError(f"Preis {value!r} muss größer als 0 sein.")
    if d > Decimal("100000"):
        raise StagingError(f"Preis {value!r} ist unplausibel hoch.")
    return d


def de_money(d: Decimal | float | None) -> str:
    if d is None:
        return "–"
    return format_de_number(float(d), 2)


def round_up_price(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 2)
    return round(math.ceil(round(value / step, 6)) * step, 2)


def build_price_file(items: list[dict], valid_from: str, valid_to: str, label: str, *, today: dt.date,
                     old_prices: dict[str, dict], header: bool) -> tuple[str, str, list[dict]]:
    if not items:
        raise StagingError("Keine Artikel angegeben.")
    if len(items) > 5000:
        raise StagingError("Zu viele Artikel (max. 5000 pro Datei).")
    vf = parse_day(valid_from, today) if valid_from else None
    vt = parse_day(valid_to, today) if valid_to else None
    if valid_from and not vf:
        raise StagingError(f"valid_from {valid_from!r} nicht verstanden (TT.MM.JJJJ).")
    if valid_to and not vt:
        raise StagingError(f"valid_to {valid_to!r} nicht verstanden (TT.MM.JJJJ).")
    if vf and vt and vf > vt:
        raise StagingError("valid_from liegt nach valid_to.")
    errors, rows, seen = [], [], set()
    for i, it in enumerate(items, 1):
        pzn, err = validate_pzn((it or {}).get("pzn"))
        if err:
            errors.append(f"Zeile {i}: {err}")
            continue
        try:
            price = _price((it or {}).get("price"))
        except StagingError as e:
            errors.append(f"Zeile {i} (PZN {pzn}): {e}")
            continue
        if pzn in seen:
            errors.append(f"Zeile {i}: PZN {pzn} doppelt.")
            continue
        seen.add(pzn)
        old = old_prices.get(pzn, {})
        rows.append({"pzn": pzn, "price": price, "old": old.get("vk"), "bezeichnung": old.get("bezeichnung"),
                     "source": old.get("source")})
    if errors:
        raise StagingError("Preisdatei nicht erstellt – bitte korrigieren:\n" + "\n".join(errors[:30]))
    lines = ["PZN;Preis"] if header else []
    lines += [f"{r['pzn']};{de_money(r['price'])}" for r in rows]
    content = "\n".join(lines) + "\n"
    md = [f"# Preisvorschlag: {label or 'ohne Titel'}", "",
          f"Gültig von **{format_de_date(vf) if vf else '–'}** bis **{format_de_date(vt) if vt else 'offen'}** "
          "(beim Import in PROKAS eintragen).", "",
          "**Nur ein Vorschlag.** Nichts wurde in PROKAS geändert. Bitte prüfen und von Hand importieren "
          "(Berichtswesen → Auswerten → Preise aus einer Excel-Tabelle übernehmen, oder Liste Artikel).", "",
          f"{len(rows)} Artikel · Format `PZN;Preis` · Windows-1252 · Dezimalkomma", "",
          "| PZN | Artikel | alt (VK) | neu | Differenz | Diff. % |", "| --- | --- | ---: | ---: | ---: | ---: |"]
    for r in rows:
        old = r["old"]
        if isinstance(old, (int, float)):
            diff = float(r["price"]) - old
            pct = f"{diff / old * 100:+.1f} %".replace(".", ",") if old else "–"
            md.append(f"| {r['pzn']} | {r['bezeichnung'] or ''} | {de_money(old)} | {de_money(r['price'])} | "
                      f"{'+' if diff >= 0 else ''}{de_money(diff)} | {pct} |")
        else:
            md.append(f"| {r['pzn']} | {r['bezeichnung'] or ''} | unbekannt | {de_money(r['price'])} | – | – |")
    md += ["", "Alte Preise stammen aus den zuletzt exportierten Listen und können veraltet sein."]
    return content, "\n".join(md) + "\n", rows


def build_order_list(supplier: str, items: list[dict], *, names: dict[str, str]) -> tuple[str, str, list[dict]]:
    if not str(supplier or "").strip():
        raise StagingError("Lieferant fehlt.")
    if not items:
        raise StagingError("Keine Artikel angegeben.")
    errors, rows, seen = [], [], {}
    for i, it in enumerate(items, 1):
        pzn, err = validate_pzn((it or {}).get("pzn"))
        if err:
            errors.append(f"Zeile {i}: {err}")
            continue
        qn = parse_number((it or {}).get("qty"))
        if qn is None or qn != int(qn) or qn <= 0:
            errors.append(f"Zeile {i} (PZN {pzn}): Menge {it.get('qty')!r} muss eine ganze Zahl > 0 sein.")
            continue
        if qn > 999:
            errors.append(f"Zeile {i} (PZN {pzn}): Menge {int(qn)} ist unplausibel hoch (max. 999).")
            continue
        if pzn in seen:
            seen[pzn]["qty"] += int(qn)
            continue
        row = {"pzn": pzn, "qty": int(qn), "bezeichnung": names.get(pzn)}
        seen[pzn] = row
        rows.append(row)
    if errors:
        raise StagingError("Bestellliste nicht erstellt – bitte korrigieren:\n" + "\n".join(errors[:30]))
    content = "PZN;Menge\n" + "".join(f"{r['pzn']};{r['qty']}\n" for r in rows)
    md = [f"# Bestellvorschlag für {supplier}", "",
          "**Nur ein Vorschlag.** Es wurde nichts bestellt. Bitte prüfen und selbst in PROKAS übernehmen bzw. senden.", "",
          f"{len(rows)} Positionen · {sum(r['qty'] for r in rows)} Packungen", "",
          "| PZN | Artikel | Menge |", "| --- | --- | ---: |"]
    md += [f"| {r['pzn']} | {r['bezeichnung'] or ''} | {r['qty']} |" for r in rows]
    return content, "\n".join(md) + "\n", rows
