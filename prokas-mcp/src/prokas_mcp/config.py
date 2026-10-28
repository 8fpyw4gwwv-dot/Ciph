"""Configuration loading: config.yaml merged over built-in defaults."""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(os.environ.get("PROKAS_MCP_HOME") or Path(__file__).resolve().parents[2])

DEFAULTS: dict[str, Any] = {
    "export_dir": r"P:\Claude\Exporte",
    "outbox_dir": r"P:\Claude\Outbox",
    "jobs_dir": r"P:\Claude\Jobs",
    "log_dir": r"P:\Claude\Logs",
    "mappings_dir": None,
    "bw_install_dir": "auto",
    "prokas_search_dirs": [r"P:\BW", r"P:\PROKAS", r"C:\PROKAS7"],
    "allowed_z_codes": [
        "Z0003", "Z0007", "Z0015", "Z0016", "Z0051", "Z0102", "Z0105", "Z0117", "Z0125", "Z0127",
        "Z0134", "Z0165", "Z0207", "Z0245", "Z0317", "Z0322", "Z0327", "Z0342", "Z0585", "Z0705",
        "Z0713", "Z0714",
    ],
    "nightly_set": ["Z0015", "Z0016", "Z0051", "Z0125", "Z0127", "Z0165", "Z0245", "Z0327"],
    "timezone": "Europe/Berlin",
    "blocked_windows": [
        {"name": "Nachtjob/Datensicherung", "days": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
         "start": "22:00", "end": "05:30"},
    ],
    "opening_hours": {"enabled": True, "days": ["Mo", "Di", "Mi", "Do", "Fr", "Sa"],
                      "start": "10:00", "end": "20:00"},
    "job_timeout_min": 20,
    "stale_after_hours": 36,
    "max_rows_returned": 500,
    "ingest_wait_sec": 300,
    "ingest_poll_sec": 3.0,
    "default_period_days": 30,
    "cli": {"enabled": True, "template": None, "test_z_code": "Z0327", "timeout_sec": 300,
            "max_failures": 3},
    "denylist_patterns": [
        "Name", "Vorname", "Kunde", "Geb", "Adresse", "Straße", "Strasse", "PLZ", "Telefon",
        r"Tel\b", "Mail", "Versicherten", "Arzt", r"Rezept-?ID", "Patient", "IBAN", "BIC",
        "Bearbeiter", "Mitarbeiter", "Benutzer",
    ],
    "default_vat_pct": 19.0,
    "price_rounding_step": 0.05,
    "price_file_header": True,
}

# Always applied, even if config.yaml leaves them out.
MANDATORY_DENYLIST = [
    "Name", "Vorname", "Kunde", "Geb", "Adresse", "Straße", "PLZ", "Telefon", "Mail",
    "Versicherten", "Arzt", r"Rezept-?ID", "Patient",
]

Z_CODE_RE = re.compile(r"^Z\d{4}$")


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    export_dir: Path
    outbox_dir: Path
    jobs_dir: Path
    log_dir: Path
    mappings_dir: Path
    bw_install_dir: str
    prokas_search_dirs: list[str]
    allowed_z_codes: list[str]
    nightly_set: list[str]
    timezone: str
    blocked_windows: list[dict]
    opening_hours: dict
    job_timeout_min: int
    stale_after_hours: float
    max_rows_returned: int
    ingest_wait_sec: float
    ingest_poll_sec: float
    default_period_days: int
    cli: dict
    denylist_patterns: list[str]
    default_vat_pct: float
    price_rounding_step: float
    price_file_header: bool
    source: str = "defaults"
    raw: dict = field(default_factory=dict, repr=False)

    def ensure_dirs(self) -> list[str]:
        """Create our own working folders. Returns problems as readable text."""
        problems = []
        for label, d in (("outbox_dir", self.outbox_dir), ("jobs_dir", self.jobs_dir),
                         ("log_dir", self.log_dir), ("export_dir", self.export_dir)):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                problems.append(f"{label} ({d}) kann nicht angelegt werden: {e}")
        return problems


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def default_config_path() -> Path:
    env = os.environ.get("PROKAS_MCP_CONFIG")
    if env:
        return Path(env)
    return PROJECT_ROOT / "config.yaml"


def _norm(p: str) -> str:
    return str(p).replace("/", "\\").rstrip("\\").lower() + "\\"


def _check_own_dirs_outside_prokas(merged: dict) -> None:
    """Hard rule: never write into PROKAS program/data folders."""
    prokas = [d for d in merged.get("prokas_search_dirs") or [] if d and "users\\public\\desktop" not in _norm(d)]
    bw = merged.get("bw_install_dir")
    if bw and str(bw).lower() != "auto":
        prokas.append(bw)
    for label in ("export_dir", "outbox_dir", "jobs_dir", "log_dir"):
        own = _norm(merged[label])
        for d in prokas:
            if own.startswith(_norm(d)):
                raise ConfigError(f"{label} ({merged[label]}) liegt im PROKAS-Ordner {d}. Der Connector schreibt "
                                  f"nie in PROKAS-Ordner – bitte einen eigenen Ordner wählen (z. B. P:\\Claude\\...).")


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    path = Path(path) if path else default_config_path()
    data: dict = {}
    source = "defaults"
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path} ist keine gültige YAML-Zuordnung.")
        source = str(path)
    merged = _deep_merge(DEFAULTS, data)
    if overrides:
        merged = _deep_merge(merged, overrides)

    codes = [str(c).strip().upper() for c in merged["allowed_z_codes"]]
    for c in codes:
        if not Z_CODE_RE.match(c):
            raise ConfigError(f"Ungültiger Z-Code in allowed_z_codes: {c!r}")
    nightly = [str(c).strip().upper() for c in merged["nightly_set"]]

    deny = list(merged["denylist_patterns"] or [])
    for p in MANDATORY_DENYLIST:
        if p not in deny:
            deny.append(p)
    for p in deny:
        try:
            re.compile(p)
        except re.error as e:
            raise ConfigError(f"Ungültiges Muster in denylist_patterns: {p!r} ({e})") from e

    _check_own_dirs_outside_prokas(merged)
    mappings_dir = merged.get("mappings_dir") or (PROJECT_ROOT / "mappings")
    cli = _deep_merge(DEFAULTS["cli"], merged.get("cli") or {})

    return Config(
        export_dir=Path(merged["export_dir"]),
        outbox_dir=Path(merged["outbox_dir"]),
        jobs_dir=Path(merged["jobs_dir"]),
        log_dir=Path(merged["log_dir"]),
        mappings_dir=Path(mappings_dir),
        bw_install_dir=str(merged["bw_install_dir"] or "auto"),
        prokas_search_dirs=[str(p) for p in merged["prokas_search_dirs"] or []],
        allowed_z_codes=codes,
        nightly_set=nightly,
        timezone=str(merged["timezone"]),
        blocked_windows=list(merged["blocked_windows"] or []),
        opening_hours=dict(merged["opening_hours"] or {}),
        job_timeout_min=int(merged["job_timeout_min"]),
        stale_after_hours=float(merged["stale_after_hours"]),
        max_rows_returned=int(merged["max_rows_returned"]),
        ingest_wait_sec=float(merged["ingest_wait_sec"]),
        ingest_poll_sec=float(merged["ingest_poll_sec"]),
        default_period_days=int(merged["default_period_days"]),
        cli=cli,
        denylist_patterns=deny,
        default_vat_pct=float(merged["default_vat_pct"]),
        price_rounding_step=float(merged["price_rounding_step"]),
        price_file_header=bool(merged["price_file_header"]),
        source=source,
        raw=merged,
    )
