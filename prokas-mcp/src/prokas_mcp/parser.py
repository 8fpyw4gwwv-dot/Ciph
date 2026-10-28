"""Turn a raw export grid into clean, redacted records for one Z-list."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .german import normalize_header, parse_date
from .mapping import HeaderMatch, ListMapping, match_headers
from .readers import RawTable, cell_text, read_raw
from .redaction import RedactionReport, Redactor

FOOTER_RE = re.compile(
    r"^\s*(summe|summen|gesamt|gesamtsumme|endsumme|zwischensumme|total|anzahl|ergebnis|"
    r"σ|∑|seite\s*\d+|datensätze|datensaetze|ende der liste|\*{2,}|={3,}|-{3,})",
    re.IGNORECASE,
)
PERIOD_RE = re.compile(
    r"(\d{1,2}\.\d{1,2}\.\d{2,4})\s*(?:-|–|bis)\s*(\d{1,2}\.\d{1,2}\.\d{2,4})", re.IGNORECASE
)
ZCODE_RE = re.compile(r"\b(Z\d{4})\b", re.IGNORECASE)
HEADER_SCAN_ROWS = 40


class ParseError(Exception):
    pass


@dataclass
class ParsedReport:
    z_code: str
    path: Path
    records: list[dict[str, Any]]
    fields: list[str]
    format: dict
    header_row: int
    match: HeaderMatch
    redactions: RedactionReport
    dropped_rows: dict[str, int]
    period: tuple[dt.date, dt.date] | None = None
    preamble_z_code: str | None = None
    warnings: list[str] = field(default_factory=list)
    mapping_validated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.records)

    def dataframe(self):
        import pandas as pd

        return pd.DataFrame(self.records, columns=self.fields)


def _nonempty(row: list[Any]) -> int:
    return sum(1 for c in row if cell_text(c))


def _looks_numeric(s: str) -> bool:
    return bool(re.fullmatch(r"[-+]?[\d.,\s%€]+-?", s))


def locate_header(rows: list[list[Any]], mapping: ListMapping | None) -> tuple[int, HeaderMatch | None]:
    """Header = the row (within the first rows) that matches the most mapping aliases.

    Without a mapping hit, fall back to the first wide row of mostly text cells.
    """
    best_idx, best_match = -1, None
    limit = min(len(rows), HEADER_SCAN_ROWS)
    if mapping:
        for i in range(limit):
            if _nonempty(rows[i]) < 2:
                continue
            m = match_headers([cell_text(c) for c in rows[i]], mapping)
            if m.score > 0 and (best_match is None or m.score > best_match.score):
                best_idx, best_match = i, m
        if best_match is not None:
            return best_idx, best_match
    widest = max((_nonempty(r) for r in rows[:limit]), default=0)
    for i in range(limit):
        cells = [cell_text(c) for c in rows[i] if cell_text(c)]
        if len(cells) >= max(2, int(widest * 0.6)) and sum(not _looks_numeric(c) for c in cells) >= len(cells) * 0.8:
            return i, None
    return -1, None


def _is_footer(row: list[Any]) -> bool:
    first = next((cell_text(c) for c in row if cell_text(c)), "")
    return bool(FOOTER_RE.match(first))


def preamble_info(rows: list[list[Any]], header_idx: int) -> tuple[str | None, tuple[dt.date, dt.date] | None]:
    z_code, period = None, None
    for row in rows[: max(header_idx, 0)]:
        line = " ".join(cell_text(c) for c in row)
        if not z_code:
            m = ZCODE_RE.search(line)
            if m:
                z_code = m.group(1).upper()
        if not period:
            m = PERIOD_RE.search(line)
            if m:
                a, b = parse_date(m.group(1)), parse_date(m.group(2))
                if a and b:
                    period = (a, b)
    return z_code, period


def parse_export(path: str | Path, mapping: ListMapping, redactor: Redactor,
                 raw: RawTable | None = None) -> ParsedReport:
    path = Path(path)
    raw = raw or read_raw(path)
    rows = raw.rows
    header_idx, match = locate_header(rows, mapping)
    if header_idx < 0 or match is None or match.score == 0:
        raise ParseError(
            f"In {path.name} wurde keine Kopfzeile mit bekannten Spalten für {mapping.z_code} "
            f"gefunden. Mit validate_mapping('{mapping.z_code}') prüfen, welche Spalten die Datei hat."
        )
    pre_z, period = preamble_info(rows, header_idx)
    warnings: list[str] = []
    if pre_z and pre_z != mapping.z_code:
        raise ParseError(f"{path.name} enthält laut Kopf die Liste {pre_z}, nicht {mapping.z_code}.")
    if match.missing_required:
        raise ParseError(
            f"{path.name}: Pflichtspalten fehlen für {mapping.z_code}: {', '.join(match.missing_required)}. "
            f"Mit validate_mapping('{mapping.z_code}') prüfen."
        )
    if match.duplicates:
        warnings.append(f"Doppelte Spaltennamen, jeweils die erste genommen: {', '.join(match.duplicates)}")

    report = RedactionReport()
    # Denylist beats allowlist: drop mapped fields whose header or name looks personal.
    columns: dict[str, int] = {}
    for fname, idx in match.columns.items():
        header = match.headers[idx]
        if redactor.is_personal(header) or redactor.is_personal(fname):
            report.personal_columns.append(header)
        else:
            columns[fname] = idx
    for h in match.unmatched:
        if redactor.is_personal(h):
            report.personal_columns.append(h)
        else:
            report.unmapped_columns.append(h)

    header_norm = [normalize_header(h) for h in match.headers]
    key_idx = columns.get(mapping.key) if mapping.key else None
    dropped = {"empty": 0, "footer": 0, "repeated_header": 0, "no_key": 0}
    records: list[dict[str, Any]] = []
    for row in rows[header_idx + 1:]:
        if _nonempty(row) == 0:
            dropped["empty"] += 1
            continue
        if [normalize_header(cell_text(c)) for c in row[: len(header_norm)]] == header_norm:
            dropped["repeated_header"] += 1
            continue
        if _is_footer(row):
            dropped["footer"] += 1
            continue
        if key_idx is not None:
            kv = row[key_idx] if key_idx < len(row) else None
            if mapping.fields[mapping.key].convert(kv) is None:
                # Typically a total/subtotal line or a wrapped text line.
                dropped["no_key"] += 1
                continue
        rec: dict[str, Any] = {}
        for fname, idx in columns.items():
            v = mapping.fields[fname].convert(row[idx] if idx < len(row) else None)
            v, changed = redactor.scrub(v)
            if changed:
                report.scrubbed_values += 1
            rec[fname] = v
        records.append(rec)

    if not mapping.validated:
        warnings.append(f"Spaltenzuordnung für {mapping.z_code} ist noch nicht an einer echten Datei bestätigt.")
    return ParsedReport(
        z_code=mapping.z_code, path=path, records=records, fields=list(columns), format=raw.describe(),
        header_row=header_idx + 1, match=match, redactions=report, dropped_rows=dropped, period=period,
        preamble_z_code=pre_z, warnings=warnings, mapping_validated=mapping.validated,
    )


def json_value(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    if isinstance(v, float):
        return round(v, 4)
    return v


def records_for_output(parsed: ParsedReport, limit: int) -> tuple[list[dict], bool]:
    out = [{k: json_value(v) for k, v in r.items()} for r in parsed.records[:limit]]
    return out, len(parsed.records) > limit
