"""Policy checks: Z-code allowlist, blocked time windows, period parsing."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .config import Config
from .german import format_de_date, parse_date

DAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
_DAY_ALIASES = {
    "mo": 0, "mon": 0, "montag": 0, "di": 1, "tue": 1, "dienstag": 1, "mi": 2, "wed": 2,
    "mittwoch": 2, "do": 3, "thu": 3, "donnerstag": 3, "fr": 4, "fri": 4, "freitag": 4,
    "sa": 5, "sat": 5, "samstag": 5, "so": 6, "sun": 6, "sonntag": 6,
}


class PolicyError(Exception):
    """A request that the rules forbid. The message is meant for the pharmacy owner."""

    code = "policy"


class NotAllowedError(PolicyError):
    code = "z_code_not_allowed"


class BlockedWindowError(PolicyError):
    code = "blocked_window"

    def __init__(self, message: str, next_allowed: dt.datetime | None, can_force: bool):
        super().__init__(message)
        self.next_allowed = next_allowed
        self.can_force = can_force


def normalize_z_code(value: str) -> str:
    s = str(value or "").strip().upper().replace(" ", "")
    m = re.fullmatch(r"Z?0*(\d{1,4})", s)
    if not m:
        raise NotAllowedError(f"{value!r} ist keine Listennummer. Erwartet z. B. Z0127.")
    return f"Z{int(m.group(1)):04d}"


def check_z_code(value: str, cfg: Config) -> str:
    z = normalize_z_code(value)
    if z not in cfg.allowed_z_codes:
        raise NotAllowedError(
            f"Liste {z} ist nicht freigegeben. Erlaubt sind nur: {', '.join(cfg.allowed_z_codes)}. "
            f"Freigabe nur durch Eintrag in allowed_z_codes (config.yaml)."
        )
    return z


def _hm(s: str) -> dt.time:
    h, m = str(s).split(":")
    return dt.time(int(h), int(m))


def _days(spec) -> set[int]:
    out = set()
    for d in spec or DAY_NAMES:
        key = str(d).strip().lower()
        if key not in _DAY_ALIASES:
            raise ValueError(f"Unbekannter Wochentag: {d!r}")
        out.add(_DAY_ALIASES[key])
    return out


def _in_window(now: dt.datetime, days: set[int], start: dt.time, end: dt.time) -> bool:
    t = now.time()
    if start <= end:
        return now.weekday() in days and start <= t < end
    # Over midnight: the part after midnight belongs to the previous day's window.
    if t >= start:
        return now.weekday() in days
    if t < end:
        return (now.weekday() - 1) % 7 in days
    return False


def active_block(now: dt.datetime, cfg: Config, force_during_opening: bool) -> tuple[str, bool] | None:
    """Returns (window name, can_force) if generating a report is blocked at `now`."""
    for w in cfg.blocked_windows:
        if _in_window(now, _days(w.get("days")), _hm(w["start"]), _hm(w["end"])):
            return str(w.get("name") or f"{w['start']}–{w['end']}"), False
    oh = cfg.opening_hours or {}
    if oh.get("enabled") and not force_during_opening:
        if _in_window(now, _days(oh.get("days")), _hm(oh["start"]), _hm(oh["end"])):
            return f"Öffnungszeit {oh['start']}–{oh['end']}", True
    return None


def next_allowed_time(now: dt.datetime, cfg: Config, force_during_opening: bool) -> dt.datetime | None:
    t = now.replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 12):  # 8 days in 5-minute steps
        t += dt.timedelta(minutes=5)
        if active_block(t, cfg, force_during_opening) is None:
            return t
    return None


def check_time_window(now: dt.datetime, cfg: Config, force_during_opening: bool = False) -> None:
    block = active_block(now, cfg, force_during_opening)
    if not block:
        return
    name, can_force = block
    nxt = next_allowed_time(now, cfg, force_during_opening)
    when = f"{DAY_NAMES[nxt.weekday()]} {nxt:%d.%m. %H:%M} Uhr" if nxt else "unbekannt"
    msg = f"Jetzt ({now:%H:%M}) werden keine Listen erzeugt: {name}. Nächstmöglich: {when}."
    if can_force:
        msg += " Während der Öffnungszeit nur mit force_during_opening=true (bremst die Kassen evtl. aus)."
    raise BlockedWindowError(msg, nxt, can_force)


# --- Periods -----------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    start: dt.date
    end: dt.date
    explicit: bool = True

    def label(self) -> str:
        return f"{format_de_date(self.start)}–{format_de_date(self.end)}"

    def as_dict(self) -> dict:
        return {"from": self.start.isoformat(), "to": self.end.isoformat(), "label": self.label()}


_MONTHS = {"januar": 1, "jan": 1, "februar": 2, "feb": 2, "märz": 3, "maerz": 3, "mrz": 3, "mär": 3,
           "april": 4, "apr": 4, "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "august": 8,
           "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "november": 11,
           "nov": 11, "dezember": 12, "dez": 12}


def _month(year: int, month: int) -> tuple[dt.date, dt.date]:
    start = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return start, nxt - dt.timedelta(days=1)


def parse_day(value: str, today: dt.date) -> dt.date | None:
    s = str(value).strip().lower()
    if s in ("heute", "today"):
        return today
    if s in ("gestern", "yesterday"):
        return today - dt.timedelta(days=1)
    return parse_date(s)


def parse_period(text: str | None, today: dt.date) -> Period | None:
    """'letzte 7 Tage', 'last_7_days', 'diesen Monat', 'letzter Monat', '2026-10', 'Oktober 2026',
    'YTD', 'Q3 2026', '01.10.2026-27.10.2026', '2026-10-01..2026-10-27', 'heute', 'gestern'."""
    if text is None or not str(text).strip():
        return None
    s = str(text).strip().lower().replace("_", " ")
    m = re.fullmatch(r"(?:letzte[nr]?|last|past)\s*(\d+)\s*(tage?|days?|d|wochen?|weeks?|w|monate?|months?)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * 7 if unit[0] == "w" else n * 30 if unit[0] == "m" else n
        return Period(today - dt.timedelta(days=days - 1), today)
    if s in ("heute", "today", "gestern", "yesterday"):
        d = parse_day(s, today)
        return Period(d, d)
    if s in ("diesen monat", "dieser monat", "this month", "lfd monat", "laufender monat", "mtd"):
        return Period(today.replace(day=1), today)
    if s in ("letzter monat", "letzten monat", "vormonat", "last month", "previous month"):
        first = today.replace(day=1)
        a, b = _month((first - dt.timedelta(days=1)).year, (first - dt.timedelta(days=1)).month)
        return Period(a, b)
    if s in ("dieses jahr", "this year", "ytd", "lfd jahr", "laufendes jahr"):
        return Period(today.replace(month=1, day=1), today)
    if s in ("letztes jahr", "vorjahr", "last year"):
        return Period(dt.date(today.year - 1, 1, 1), dt.date(today.year - 1, 12, 31))
    if s in ("diese woche", "this week"):
        return Period(today - dt.timedelta(days=today.weekday()), today)
    if s in ("letzte woche", "last week", "vorwoche"):
        start = today - dt.timedelta(days=today.weekday() + 7)
        return Period(start, start + dt.timedelta(days=6))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", s)
    if m:
        return Period(*_month(int(m.group(1)), int(m.group(2))))
    m = re.fullmatch(r"(\d{1,2})[./](\d{4})", s)
    if m:
        return Period(*_month(int(m.group(2)), int(m.group(1))))
    m = re.fullmatch(r"([a-zäöü]+)\.?\s*(\d{4})?", s)
    if m and m.group(1) in _MONTHS:
        year = int(m.group(2)) if m.group(2) else today.year
        return Period(*_month(year, _MONTHS[m.group(1)]))
    m = re.fullmatch(r"q([1-4])\s*(\d{4})?", s)
    if m:
        q, year = int(m.group(1)), int(m.group(2) or today.year)
        a, _ = _month(year, 3 * q - 2)
        _, b = _month(year, 3 * q)
        return Period(a, b)
    parts = re.split(r"\s*(?:\.\.|–|—|\bbis\b|\bto\b|\s-\s|(?<=\d)-(?=\d{1,2}\.\d{1,2}\.)|(?<=\d{4})-(?=\d{4}-))\s*", s)
    if len(parts) == 2 and all(re.fullmatch(r"[\d.\-/]+|heute|gestern|today|yesterday", x) for x in parts):
        a, b = parse_day(parts[0], today), parse_day(parts[1], today)
        if a and b:
            return Period(min(a, b), max(a, b))
    if re.fullmatch(r"[\d.\-/]+", s):
        d = parse_day(s, today)
        if d:
            return Period(d, d)
    raise PolicyError(
        f"Zeitraum {text!r} nicht verstanden. Beispiele: 'letzte 7 Tage', 'letzter Monat', "
        f"'Oktober 2026', '01.10.2026-27.10.2026'."
    )


def resolve_period(today: dt.date, cfg: Config, *, period: str | None = None, date_from: str | None = None,
                   date_to: str | None = None, needs_period: bool = True) -> Period:
    if date_from or date_to:
        a = parse_day(date_from, today) if date_from else None
        b = parse_day(date_to, today) if date_to else None
        if date_from and not a:
            raise PolicyError(f"Datum {date_from!r} nicht verstanden (TT.MM.JJJJ).")
        if date_to and not b:
            raise PolicyError(f"Datum {date_to!r} nicht verstanden (TT.MM.JJJJ).")
        a = a or (b - dt.timedelta(days=cfg.default_period_days - 1))
        b = b or today
        if a > b:
            raise PolicyError(f"Zeitraum verkehrt: {format_de_date(a)} liegt nach {format_de_date(b)}.")
        p = Period(a, b)
    else:
        p = parse_period(period, today)
    if p is None:
        if needs_period:
            return Period(today - dt.timedelta(days=cfg.default_period_days - 1), today, explicit=False)
        return Period(today, today, explicit=False)
    if p.end > today:
        p = Period(p.start, today, p.explicit)
    if p.start > today:
        raise PolicyError("Der Zeitraum liegt in der Zukunft.")
    if (p.end - p.start).days > 3 * 366:
        raise PolicyError("Zeitraum zu lang (max. 3 Jahre).")
    return p
