"""Anonymised, synthetic exports for every mapped list.

Each fixture uses plausible Berichtswesen headers, German number/date formats, a
title block, a total line and at least one personal column (fake person
"Erika Mustermann") that must never leave the server.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

PERSON = "Mustermann"
PERSON_FULL = "Erika Mustermann"
PERSON_MAIL = "erika.mustermann@example.de"


def make_pzn(seed: int) -> str:
    base = 1000000 + seed * 7919
    while True:
        s = f"{base:07d}"[-7:]
        total = sum(int(d) * w for d, w in zip(s, range(1, 8)))
        if total % 11 != 10:
            return s + str(total % 11)
        base += 1


P = [make_pzn(i) for i in range(1, 12)]

# z_code -> (headers, rows). Values are strings as written by "Export in Editor".
FIXTURES: dict[str, tuple[list[str], list[list[str]]]] = {
    "Z0003": (["Erlösgruppe", "Umsatz netto", "Rohertrag", "Spanne %", "Packungen", "Anteil %"],
              [["Rx", "45.000,00", "9.000,00", "20,0", "1.500", "60,0"],
               ["OTC", "20.000,00", "7.000,00", "35,0", "2.000", "26,7"],
               ["Freiwahl", "10.000,00", "4.000,00", "40,0", "1.800", "13,3"]]),
    "Z0007": (["Großhändler", "Einkaufswert", "Positionen", "Packungen", "Rabatt %"],
              [["Gehe", "30.000,00", "800", "2.500", "3,5"], ["Alliance", "12.500,50", "300", "900", "2,0"]]),
    "Z0015": (["Datum", "PZN", "Artikelbezeichnung", "Packungsgröße", "Anbieter", "Menge", "Art", "Kunde", "Bearbeiter"],
              [["20.10.2026", P[0], "Ibuprofen 400 Filmtabl.", "20 St", "Hexal", "2", "Bestellung", PERSON_FULL, "MA1"],
               ["21.10.2026", P[0], "Ibuprofen 400 Filmtabl.", "20 St", "Hexal", "1", "Neinverkauf", PERSON_FULL, "MA2"],
               ["22.10.2026", P[1], "Nasenspray 0,1%", "10 ml", "Ratiopharm", "3", "Neinverkauf", "", "MA1"],
               ["23.10.2026", P[2], "Vitamin D3 1000 IE", "100 St", "Dr. Muster", "1", "Bestellung", PERSON_FULL, "MA3"]]),
    "Z0016": (["Datum", "PZN", "Bezeichnung", "PG", "Hersteller", "Anzahl", "Grund", "Patient"],
              [["21.10.2026", P[0], "Ibuprofen 400 Filmtabl.", "20 St", "Hexal", "1", "nicht lieferbar", PERSON_FULL],
               ["22.10.2026", P[1], "Nasenspray 0,1%", "10 ml", "Ratiopharm", "3", "defekt", PERSON_FULL]]),
    "Z0051": (["PZN", "Artikelbezeichnung", "Packungsgröße", "Anbieter", "Verfalldatum", "Bestand", "Listen-EK", "Lagerort"],
              [[P[3], "Augentropfen", "10 ml", "Ursapharm", "30.11.2026", "4", "5,20", "Sichtwahl"],
               [P[4], "Insulin Pen", "5x3 ml", "Novo", "12/2026", "2", "80,00", "Kühlschrank"],
               [P[5], "Hustensaft", "100 ml", "Klosterfrau", "15.01.2027", "6", "4,10", "Freiwahl"],
               [P[6], "Probiotikum", "30 St", "Muster AG", "20.10.2026", "1", "12,00", "Freiwahl"]]),
    "Z0102": (["Hersteller", "PZN", "Bezeichnung", "Bestand", "EK alt", "EK neu", "Gültig ab", "Verlust"],
              [["Hexal", P[0], "Ibuprofen 400", "10", "3,00", "2,50", "15.10.2026", "5,00"]]),
    "Z0105": (["Lagerort", "Lagerwert", "Wareneinsatz", "Lagerumschlag", "Lagerdauer Tage", "Anzahl Artikel"],
              [["Sichtwahl", "40.000,00", "400.000,00", "10,0", "36", "3.000"],
               ["Freiwahl", "10.000,00", "50.000,00", "5,0", "73", "900"]]),
    "Z0117": (["PZN", "Artikelbezeichnung", "PG", "Bestand", "Listen-EK", "VEK", "Bereinigter VEK", "Lagerwert"],
              [[P[7], "Salbe", "50 g", "3", "6,00", "6,50", "6,00", "19,50"]]),
    "Z0125": (["PZN", "Artikelbezeichnung", "Packungsgröße", "Anbieter", "Lagerort", "Bestand", "Listen-EK", "Eigener VK",
               "Spanne %", "MwSt %", "AMO", "Rezeptpflicht"],
              [[P[0], "Ibuprofen 400 Filmtabl.", "20 St", "Hexal", "Freiwahl", "12", "2,50", "3,49", "14,8", "19", "8", "nein"],
               [P[1], "Nasenspray 0,1%", "10 ml", "Ratiopharm", "Freiwahl", "5", "2,10", "2,95", "", "19", "4", "nein"],
               [P[8], "Pflaster", "10 St", "Hansaplast", "Freiwahl", "20", "1,80", "2,49", "14,0", "19", "6", ""],
               [P[9], "Rx-Test", "20 St", "Muster", "Freiwahl", "1", "10,00", "11,00", "", "19", "1", "Rx"]]),
    "Z0127": (["PZN", "Artikelbezeichnung", "Packungsgröße", "Anbieter", "Lagerort", "Bestand", "Listen-EK", "VK",
               "Letzter Verkauf", "Letzter WE", "Lagerwert"],
              [[P[4], "Insulin Pen", "5x3 ml", "Novo", "Kühlschrank", "2", "80,00", "120,00", "01.07.2026", "01.05.2026", "160,00"],
               [P[10], "Kompressionsstrumpf", "1 Paar", "Medi", "Lager", "3", "55,00", "89,00", "15.08.2026", "01.03.2026", "165,00"],
               [P[2], "Spezialnahrung", "500 g", "Nutricia", "Lager", "1", "60,00", "79,00", "", "01.01.2026", "60,00"]]),
    "Z0134": (["PZN", "Artikelbezeichnung", "PG", "Anbieter", "Listen-EK", "VEK", "VK", "Eigener Listen-EK"],
              [[P[6], "Eigenherstellung", "100 ml", "Apotheke", "0,00", "0,00", "8,50", "N"]]),
    "Z0165": (["Status", "Umsatz", "Rohertrag", "Spanne %", "Abgaben", "Umsatz VJ"],
              [["Rx", "45.000,00", "9.000,00", "20,0", "1.500", "42.000,00"],
               ["OTC", "30.000,00", "11.000,00", "36,7", "3.800", "29.000,00"]]),
    "Z0207": (["Lieferant", "PZN", "Artikelbezeichnung", "PG", "Anbieter", "Menge", "Retourendatum", "Retourenwert", "Grund"],
              [["Gehe", P[3], "Augentropfen", "10 ml", "Ursapharm", "2", "01.10.2026", "10,40", "Verfall"],
               ["Gehe", P[5], "Hustensaft", "100 ml", "Klosterfrau", "1", "10.10.2026", "4,10", "Falschlieferung"],
               ["Alliance", P[7], "Salbe", "50 g", "Muster", "1", "20.10.2026", "6,00", "Bruch"]]),
    "Z0245": (["Monat", "Nachfragen", "Abgaben", "Defektquote %", "Lieferfähigkeit %"],
              [["09/2026", "150", "10.000", "1,5", "98,5"], ["10/2026", "250", "10.000", "2,5", "97,5"]]),
    "Z0317": (["Datum", "PZN", "Artikelbezeichnung", "PG", "Preistyp", "Alter Preis", "Neuer Preis", "Bearbeiter"],
              [["20.10.2026", P[0], "Ibuprofen 400", "20 St", "VK", "3,29", "3,49", PERSON_FULL]]),
    "Z0322": (["PZN", "Artikelbezeichnung", "PG", "Bestand", "Listen-EK", "VEK", "Lagerwert", "Lagerort"],
              [[P[4], "Insulin Pen", "5x3 ml", "8", "80,00", "79,00", "640,00", "Kühlschrank"]]),
    "Z0327": (["PZN", "Artikelbezeichnung", "PG", "Anbieter", "Bestand", "Listen-EK", "VEK", "VK", "Lagerort"],
              [[P[6], "Rezeptur Basis", "100 g", "Apotheke", "2", "0,00", "0,00", "5,00", "Labor"],
               [P[8], "Muster ohne EK", "1 St", "Muster", "0", "", "", "1,00", "Lager"]]),
    "Z0342": (["PZN", "Artikelbezeichnung", "PG", "Anbieter", "Bestand", "Lagerort"],
              [[P[9], "Sirup", "200 ml", "Muster", "3", "Sichtwahl"]]),
    "Z0585": (["PZN", "Artikelbezeichnung", "PG", "Anbieter", "EK", "VK", "Spanne", "MwSt", "Abgaben", "Umsatz", "Rohertrag"],
              [[P[1], "Nasenspray 0,1%", "10 ml", "Ratiopharm", "2,10", "2,95", "", "19", "40", "99,16", "15,16"],
               [P[10], "Kompressionsstrumpf", "1 Paar", "Medi", "55,00", "89,00", "26,5", "19", "2", "149,58", "39,58"]]),
    "Z0705": (["Kunde", "Name", "Rechnungsnummer", "Rechnungsdatum", "Fällig am", "Rechnungsbetrag", "Offener Betrag",
               "Mahnstufe", "E-Mail"],
              [["10001", PERSON_FULL, "R-2026-001", "01.06.2026", "01.07.2026", "120,00", "120,00", "2", PERSON_MAIL],
               ["10002", "Max " + PERSON, "R-2026-050", "01.10.2026", "31.10.2026", "50,00", "20,00", "0", ""],
               ["10003", "Firma Beispiel", "R-2026-020", "01.08.2026", "31.08.2026", "300,00", "300,00", "1", ""]]),
    "Z0713": (["PZN", "Artikelbezeichnung", "PG", "Anbieter", "Bestand", "AMO", "MB", "Listen-EK", "VK", "Lagerort", "Status"],
              [[P[0], "Ibuprofen 400 Filmtabl.", "20 St", "Hexal", "0", "8", "4", "2,50", "3,49", "Sichtwahl", "rot"]]),
    "Z0714": (["PZN", "Artikelbezeichnung", "PG", "Anbieter", "Bestand", "AMO", "MB", "Listen-EK", "VK", "Lagerort", "Status", "Ab"],
              [[P[1], "Nasenspray 0,1%", "10 ml", "Ratiopharm", "1", "4", "2", "2,10", "2,95", "Freiwahl", "gelb", "01.11.2026"]]),
}

# Lists where the fixture has a total line ("Summe") we expect to be removed.
TOTALS = {"Z0003", "Z0007", "Z0105", "Z0165", "Z0245", "Z0705", "Z0127", "Z0051"}


def _total_row(headers: list[str]) -> list[str]:
    row = ["Summe"] + [""] * (len(headers) - 1)
    return row


def write_txt(z: str, directory: Path, name: str | None = None, *, period: tuple[str, str] | None = None,
              encoding: str = "cp1252", sep: str = "\t", mtime: dt.datetime | None = None,
              page_break: bool = False) -> Path:
    headers, rows = FIXTURES[z]
    lines = ["easyApotheke im Nedderfeld Center – Berichtswesen", f"Liste {z}"]
    if period:
        lines.append(f"Berichtszeitraum: {period[0]} - {period[1]}")
    lines.append("")
    lines.append(sep.join(headers))
    for i, r in enumerate(rows):
        if page_break and i == 1:
            lines += ["\x0cSeite 2", sep.join(headers)]
        lines.append(sep.join(r))
    if z in TOTALS:
        lines.append(sep.join(_total_row(headers)))
    lines.append("")
    path = Path(directory) / (name or f"{z}_export.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("\r\n".join(lines).encode(encoding))
    _touch(path, mtime)
    return path


def write_xlsx(z: str, directory: Path, name: str | None = None, *, mtime: dt.datetime | None = None,
               native: bool = True) -> Path:
    """Excel export as from 'Auswerten -> Excel starten': title rows, then a grid.
    With native=True numbers/dates are real Excel values (as Excel converts them)."""
    import openpyxl

    from prokas_mcp.german import parse_date, parse_number

    headers, rows = FIXTURES[z]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = z
    ws.append([f"Liste {z}"])
    ws.append([])
    ws.append(headers)
    for r in rows:
        out = []
        for h, v in zip(headers, r):
            if native and v and "PZN" not in h and not any(c.isalpha() for c in v.replace("%", "")):
                d = parse_date(v) if v.count(".") == 2 and len(v) == 10 else None
                n = parse_number(v) if d is None and "/" not in v else None
                out.append(d if d else n if n is not None else v)
            elif native and "PZN" in h:
                out.append(int(v))  # Excel drops leading zeros
            else:
                out.append(v)
        ws.append(out)
    if z in TOTALS:
        ws.append(_total_row(headers))
    path = Path(directory) / (name or f"{z}_export.xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    _touch(path, mtime)
    return path


def _touch(path: Path, mtime: dt.datetime | None) -> None:
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
