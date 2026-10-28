from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from prokas_mcp.app import App
from prokas_mcp.config import PROJECT_ROOT, load_config

TZ = ZoneInfo("Europe/Berlin")
# Tuesday 07:00 - outside the Nachtjob window and before opening hours.
DEFAULT_NOW = dt.datetime(2026, 10, 27, 7, 0, tzinfo=TZ)


class Clock:
    def __init__(self, now: dt.datetime = DEFAULT_NOW):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def make_cfg(tmp_path: Path, **overrides):
    base = {
        "export_dir": str(tmp_path / "Exporte"),
        "outbox_dir": str(tmp_path / "Outbox"),
        "jobs_dir": str(tmp_path / "Jobs"),
        "log_dir": str(tmp_path / "Logs"),
        "mappings_dir": str(PROJECT_ROOT / "mappings"),
        "prokas_search_dirs": [str(tmp_path / "PROKAS")],
        "ingest_wait_sec": 1.0,
        "ingest_poll_sec": 0.05,
    }
    base.update(overrides)
    cfg = load_config(tmp_path / "no-config.yaml", overrides=base)
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def app(cfg, clock):
    return App(cfg, clock=clock)


@pytest.fixture
def fresh(clock):
    """mtime one hour before the test clock."""
    return clock.now - dt.timedelta(hours=1)
