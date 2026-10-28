"""German number, date and PZN handling for Berichtswesen exports."""

from __future__ import annotations

import calendar
import datetime as dt
import re
import unicodedata
from typing import Any

_THOUSANDS_DOT = re.compile(r"^\d{1,3}(\.\d{3})+$")
_CURRENCY = re.compile(r"(€|eur|euro|%|stk\.?|stück|pck\.?)", re.IGNORECASE)


def parse_number(value: Any) -> float | None:
    """Parse '1.234,56', '-12,5 %', '3,50 €', '1.234,56-' or real numbers. None if not a number."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:  # NaN
            return None
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    s = _CURRENCY.sub("", s).replace(" ", "").replace(" ", "").replace("'", "")
    if not s or s in {"-", "--", "+"}:
        return None
    negative = False
    if s.endswith("-") and len(s) > 1:
        negative, s = True, s[:-1]
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif _THOUSANDS_DOT.match(s):
        s = s.replace(".", "")
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negative else n


def parse_int(value: Any) -> int | None:
    n = parse_number(value)
    if n is None:
        return None
    return int(round(n))


_DATE_PATTERNS = [
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})"), "dmy"),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2})$"), "dmy2"),
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})"), "ymd"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "dmy"),
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), "ymd"),
]


def _two_digit_year(y: int) -> int:
    return 2000 + y if y < 70 else 1900 + y


def parse_date(value: Any) -> dt.date | None:
    """Parse 'TT.MM.JJJJ', 'TT.MM.JJ', ISO dates, Excel datetimes. None if not a date."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for rx, kind in _DATE_PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        try:
            if kind == "dmy":
                return dt.date(c, b, a)
            if kind == "dmy2":
                return dt.date(_two_digit_year(c), b, a)
            return dt.date(a, b, c)
        except ValueError:
            return None
    return None


_EXPIRY_MY = re.compile(r"^(\d{1,2})[./-](\d{2}|\d{4})$")


def parse_expiry(value: Any) -> dt.date | None:
    """Expiry dates: full dates, or month-only 'MM/JJJJ', 'MM.JJ' -> last day of that month."""
    d = parse_date(value)
    if d:
        return d
    if value is None:
        return None
    m = _EXPIRY_MY.match(str(value).strip())
    if not m:
        return None
    month, year = int(m.group(1)), int(m.group(2))
    if year < 100:
        year = _two_digit_year(year)
    if not 1 <= month <= 12:
        return None
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def format_de_number(n: float, decimals: int = 2) -> str:
    s = f"{n:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def format_de_date(d: dt.date) -> str:
    return d.strftime("%d.%m.%Y")


def normalize_header(h: Any) -> str:
    """Compare-friendly header: lowercase, umlauts folded, only letters/digits."""
    s = str(h or "").strip().lower()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if ch.isalnum())
    return s


# --- PZN -------------------------------------------------------------------

def pzn_check_digit_ok(pzn8: str) -> bool:
    """PZN-8: sum(d_i * i, i=1..7) mod 11 must equal the 8th digit (10 is never valid)."""
    if not re.fullmatch(r"\d{8}", pzn8):
        return False
    total = sum(int(d) * w for d, w in zip(pzn8[:7], range(1, 8)))
    check = total % 11
    return check != 10 and check == int(pzn8[7])


def normalize_pzn(value: Any) -> str | None:
    """Return an 8-digit PZN string (old 7-digit PZN get a leading 0), or None."""
    if value is None:
        return None
    if isinstance(value, float):
        if value != value or value <= 0 or not value.is_integer():
            return None
        s = str(int(value))
    else:
        s = re.sub(r"[\s-]", "", str(value).strip())
        if s.upper().startswith("PZN"):
            s = s[3:]
        if s.endswith(".0"):
            s = s[:-2]
    if not s.isdigit():
        return None
    if len(s) < 7:
        s = s.zfill(7)
    if len(s) == 7:
        s = "0" + s
    if len(s) != 8:
        return None
    return s


def validate_pzn(value: Any) -> tuple[str | None, str | None]:
    """Returns (pzn8, error). Accepts 7 or 8 digits with a valid check digit."""
    raw = re.sub(r"\s", "", str(value or ""))
    if raw.upper().startswith("PZN"):
        raw = raw[3:].lstrip(":-")
    if not re.fullmatch(r"\d{7,8}", raw):
        return None, f"PZN {value!r} muss 7 oder 8 Ziffern haben."
    pzn = raw if len(raw) == 8 else "0" + raw
    if not pzn_check_digit_ok(pzn):
        return None, f"PZN {value!r} hat eine ungültige Prüfziffer."
    return pzn, None
