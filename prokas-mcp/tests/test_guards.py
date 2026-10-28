import datetime as dt

import pytest

from conftest import TZ, make_cfg
from prokas_mcp.guards import (BlockedWindowError, NotAllowedError, PolicyError, active_block, check_time_window,
                               check_z_code, next_allowed_time, parse_period, resolve_period)


def at(day: int, hh: int, mm: int = 0) -> dt.datetime:
    # 2026-10-26 is a Monday.
    return dt.datetime(2026, 10, 26, hh, mm, tzinfo=TZ) + dt.timedelta(days=day)


@pytest.mark.parametrize("raw,expected", [("Z0127", "Z0127"), ("z127", "Z0127"), ("127", "Z0127"),
                                          (" Z 0015 ", "Z0015"), ("Z0003", "Z0003")])
def test_allowlist_accepts_and_normalizes(tmp_path, raw, expected):
    assert check_z_code(raw, make_cfg(tmp_path)) == expected


@pytest.mark.parametrize("raw", ["Z0999", "Z0012", "Z0145", "Z0073", "rm -rf", "", "Z12345"])
def test_allowlist_rejects(tmp_path, raw):
    with pytest.raises(NotAllowedError):
        check_z_code(raw, make_cfg(tmp_path))


def test_allowlist_message_is_clear(tmp_path):
    with pytest.raises(NotAllowedError, match="nicht freigegeben"):
        check_z_code("Z0145", make_cfg(tmp_path))


@pytest.mark.parametrize("when,blocked", [
    (at(1, 22, 0), True), (at(1, 23, 59), True), (at(2, 3, 0), True), (at(2, 5, 29), True),
    (at(2, 5, 30), False), (at(2, 9, 59), False), (at(1, 21, 59), False),
    (at(6, 23, 0), True), (at(0, 2, 0), True),  # Sunday night and Monday early (from Sunday's window)
])
def test_nightjob_window(tmp_path, when, blocked):
    cfg = make_cfg(tmp_path)
    b = active_block(when, cfg, force_during_opening=True)
    assert (b is not None) == blocked
    if blocked:
        assert b[1] is False  # never forceable


def test_nightjob_cannot_be_forced(tmp_path):
    with pytest.raises(BlockedWindowError) as e:
        check_time_window(at(1, 23), make_cfg(tmp_path), force_during_opening=True)
    assert not e.value.can_force
    assert e.value.next_allowed == at(2, 5, 30)


def test_opening_hours_need_force(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(BlockedWindowError) as e:
        check_time_window(at(1, 11), cfg)
    assert e.value.can_force and "force_during_opening" in str(e.value)
    assert e.value.next_allowed == at(1, 20, 0)
    check_time_window(at(1, 11), cfg, force_during_opening=True)  # no exception
    check_time_window(at(6, 11), cfg)  # Sunday: closed, allowed
    check_time_window(at(5, 20, 0), cfg)  # Saturday 20:00 allowed


def test_opening_hours_can_be_disabled(tmp_path):
    cfg = make_cfg(tmp_path, opening_hours={"enabled": False})
    check_time_window(at(1, 11), cfg)


def test_custom_windows(tmp_path):
    cfg = make_cfg(tmp_path, blocked_windows=[{"name": "Wartung", "days": ["Mi"], "start": "12:00", "end": "13:00"}])
    with pytest.raises(BlockedWindowError, match="Wartung"):
        check_time_window(at(2, 12, 30), cfg, force_during_opening=True)
    assert next_allowed_time(at(2, 12, 30), cfg, True) == at(2, 13, 0)


TODAY = dt.date(2026, 10, 27)


@pytest.mark.parametrize("text,start,end", [
    ("letzte 7 Tage", dt.date(2026, 10, 21), TODAY),
    ("last_7_days", dt.date(2026, 10, 21), TODAY),
    ("letzter Monat", dt.date(2026, 9, 1), dt.date(2026, 9, 30)),
    ("diesen Monat", dt.date(2026, 10, 1), TODAY),
    ("2026-09", dt.date(2026, 9, 1), dt.date(2026, 9, 30)),
    ("Oktober 2026", dt.date(2026, 10, 1), dt.date(2026, 10, 31)),
    ("märz", dt.date(2026, 3, 1), dt.date(2026, 3, 31)),
    ("Q3 2026", dt.date(2026, 7, 1), dt.date(2026, 9, 30)),
    ("YTD", dt.date(2026, 1, 1), TODAY),
    ("01.10.2026-27.10.2026", dt.date(2026, 10, 1), TODAY),
    ("01.10.2026 bis 15.10.2026", dt.date(2026, 10, 1), dt.date(2026, 10, 15)),
    ("2026-10-01..2026-10-15", dt.date(2026, 10, 1), dt.date(2026, 10, 15)),
    ("gestern", dt.date(2026, 10, 26), dt.date(2026, 10, 26)),
    ("letzte Woche", dt.date(2026, 10, 19), dt.date(2026, 10, 25)),
])
def test_parse_period(text, start, end):
    p = parse_period(text, TODAY)
    assert (p.start, p.end) == (start, end)


def test_parse_period_rejects_nonsense():
    with pytest.raises(PolicyError, match="nicht verstanden"):
        parse_period("irgendwann", TODAY)


def test_resolve_period_defaults_and_limits(tmp_path):
    cfg = make_cfg(tmp_path)
    p = resolve_period(TODAY, cfg)
    assert not p.explicit and p.end == TODAY and (p.end - p.start).days == 29
    p = resolve_period(TODAY, cfg, needs_period=False)
    assert p.start == p.end == TODAY
    p = resolve_period(TODAY, cfg, period="Oktober 2026")
    assert p.end == TODAY  # clipped to today
    with pytest.raises(PolicyError):
        resolve_period(TODAY, cfg, date_from="20.10.2026", date_to="01.10.2026")
    with pytest.raises(PolicyError):
        resolve_period(TODAY, cfg, period="Dezember 2027")


def test_own_folders_must_not_be_inside_prokas(tmp_path):
    from prokas_mcp.config import ConfigError

    with pytest.raises(ConfigError, match="PROKAS-Ordner"):
        make_cfg(tmp_path, prokas_search_dirs=[r"P:\BW"], outbox_dir=r"P:\BW\Claude\Outbox")
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, bw_install_dir=r"C:\PROKAS7\bw", jobs_dir=r"c:/prokas7/BW/jobs")
    make_cfg(tmp_path, prokas_search_dirs=[r"P:\BW"], outbox_dir=r"P:\BWX\Outbox")  # sibling is fine


def test_no_database_or_network_code():
    """Read-only toward PROKAS: no DB2 driver, no network, no mail anywhere in the connector."""
    import re
    from prokas_mcp.config import PROJECT_ROOT

    src = "\n".join(p.read_text(encoding="utf-8") for p in (PROJECT_ROOT / "src" / "prokas_mcp").glob("*.py"))
    for forbidden in (r"import ibm_db", r"pyodbc", r"jaydebeapi", r"import socket", r"import requests", r"smtplib",
                      r"urllib\.request", r"httpx"):
        assert not re.search(forbidden, src), forbidden
