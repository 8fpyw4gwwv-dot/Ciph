import datetime as dt

import pytest

from prokas_mcp.german import (format_de_number, normalize_header, normalize_pzn, parse_date, parse_expiry,
                               parse_number, pzn_check_digit_ok, validate_pzn)


@pytest.mark.parametrize("raw,expected", [
    ("1.234,56", 1234.56),
    ("1234,56", 1234.56),
    ("-1.234,56", -1234.56),
    ("1.234,56-", -1234.56),
    ("(12,50)", -12.5),
    ("12,5 %", 12.5),
    ("3,50 €", 3.5),
    ("EUR 3,50", 3.5),
    ("1.000.000", 1000000.0),
    ("1.234", 1234.0),
    ("0,1", 0.1),
    ("1.5", 1.5),          # English decimal stays a decimal
    ("  7  ", 7.0),
    (" 1 234,00 ", 1234.0),
    (12, 12.0),
    (3.25, 3.25),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "-", "abc", "12 St", True, float("nan")])
def test_parse_number_rejects(raw):
    assert parse_number(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("27.10.2026", dt.date(2026, 10, 27)),
    ("1.2.2026", dt.date(2026, 2, 1)),
    ("27.10.26", dt.date(2026, 10, 27)),
    ("2026-10-27", dt.date(2026, 10, 27)),
    ("20261027", dt.date(2026, 10, 27)),
    (dt.datetime(2026, 10, 27, 13, 5), dt.date(2026, 10, 27)),
    ("27.10.2026 13:45", dt.date(2026, 10, 27)),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["31.02.2026", "", "Summe", "10/2026", None])
def test_parse_date_rejects(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("12/2026", dt.date(2026, 12, 31)),
    ("02.27", dt.date(2027, 2, 28)),
    ("02/2028", dt.date(2028, 2, 29)),
    ("15.01.2027", dt.date(2027, 1, 15)),
])
def test_parse_expiry(raw, expected):
    assert parse_expiry(raw) == expected


def test_pzn_check_digit():
    # Published examples: PZN-8 27580899 and the old 7-digit PZN 4908802.
    assert pzn_check_digit_ok("27580899")
    assert validate_pzn("4908802")[0] == "04908802"
    pzn, err = validate_pzn("27580898")
    assert pzn is None and "Prüfziffer" in err
    assert validate_pzn("12")[1]
    assert validate_pzn("PZN 27580899")[0] == "27580899"


def test_normalize_pzn_restores_leading_zero():
    assert normalize_pzn(4908802) == "04908802"      # Excel dropped the leading zero
    assert normalize_pzn(4908802.0) == "04908802"
    assert normalize_pzn("4908802") == "04908802"
    assert normalize_pzn("Summe") is None
    assert normalize_pzn("") is None


def test_normalize_header():
    assert normalize_header("Listen-EK") == normalize_header("listen ek") == "listenek"
    assert normalize_header("Größe") == "groesse"
    assert normalize_header(" Spanne % ") == "spanne"


def test_format_de_number():
    assert format_de_number(1234.5) == "1.234,50"
    assert format_de_number(-0.5) == "-0,50"
