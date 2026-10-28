"""Index of export files: which Z-list, which period, how old."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from .german import parse_date
from .readers import SUPPORTED_EXT, decode_bytes

_Z_IN_NAME = re.compile(r"(?<![A-Za-z0-9])Z[_-]?(\d{4})(?!\d)", re.IGNORECASE)
_DATE_TOKENS = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{8}|\d{2}\.\d{2}\.\d{4})")
_PERIOD_IN_TEXT = re.compile(
    r"(\d{1,2}\.\d{1,2}\.\d{2,4})\s*(?:-|–|bis)\s*(\d{1,2}\.\d{1,2}\.\d{2,4})", re.IGNORECASE
)
_Z_IN_TEXT = re.compile(r"\b(Z\d{4})\b")


@dataclass
class ExportFile:
    path: Path
    z_code: str | None
    period: tuple[dt.date, dt.date] | None
    modified: dt.datetime

    def age_hours(self, now: dt.datetime) -> float:
        return (now - self.modified).total_seconds() / 3600

    def public(self, now: dt.datetime) -> dict:
        return {
            "file": self.path.name,
            "z_code": self.z_code,
            "period": (f"{self.period[0].isoformat()} – {self.period[1].isoformat()}" if self.period else None),
            "modified": self.modified.isoformat(timespec="minutes"),
            "age_hours": round(self.age_hours(now), 1),
        }


def _date_token(tok: str) -> dt.date | None:
    if re.fullmatch(r"\d{8}", tok):
        try:
            return dt.date(int(tok[:4]), int(tok[4:6]), int(tok[6:]))
        except ValueError:
            return None
    return parse_date(tok)


def info_from_name(name: str) -> tuple[str | None, tuple[dt.date, dt.date] | None]:
    m = _Z_IN_NAME.search(name)
    z = f"Z{m.group(1)}" if m else None
    dates = [d for d in (_date_token(t) for t in _DATE_TOKENS.findall(name)) if d]
    period = (min(dates[0], dates[1]), max(dates[0], dates[1])) if len(dates) >= 2 else None
    return z, period


def _peek_text(path: Path) -> tuple[str | None, tuple[dt.date, dt.date] | None]:
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None, None
    text, _ = decode_bytes(head)
    z = None
    m = _Z_IN_TEXT.search(text)
    if m:
        z = m.group(1)
    period = None
    m = _PERIOD_IN_TEXT.search(text)
    if m:
        a, b = parse_date(m.group(1)), parse_date(m.group(2))
        if a and b:
            period = (a, b)
    return z, period


def scan_exports(dirs: list[Path], tz: dt.tzinfo | None = None) -> list[ExportFile]:
    out: list[ExportFile] = []
    for d in dirs:
        if not d or not Path(d).exists():
            continue
        for p in Path(d).iterdir():
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXT or p.name.startswith(("~$", ".")):
                continue
            z, period = info_from_name(p.name)
            if (z is None or period is None) and p.suffix.lower() not in (".xlsx", ".xlsm", ".xls"):
                z2, p2 = _peek_text(p)
                z, period = z or z2, period or p2
            try:
                mtime = dt.datetime.fromtimestamp(p.stat().st_mtime, tz=tz).astimezone(tz)
            except OSError:
                continue
            out.append(ExportFile(p, z.upper() if z else None, period, mtime))
    out.sort(key=lambda e: e.modified, reverse=True)
    return out


def latest_by_code(files: list[ExportFile]) -> dict[str, ExportFile]:
    latest: dict[str, ExportFile] = {}
    for f in files:  # newest first
        if f.z_code and f.z_code not in latest:
            latest[f.z_code] = f
    return latest
