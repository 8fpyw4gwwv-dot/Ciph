"""Headless ("CLI") Berichtswesen runs: discovery, probe and execution.

PROKAS has no documented command line for Berichtswesen. probe_cli therefore only
*looks* (read-only) for launchers whose text shows list-number / period / output
parameters, and only tests a candidate with the harmless check list (Z0327 by
default), a one-day period and output into jobs_dir. Anything whose name or text
hints at changing data is never executed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .jobs import write_json_atomic

SCAN_EXT = {".rex", ".cmd", ".bat", ".jar", ".ini", ".properties", ".cfg", ".conf", ".xml", ".txt", ".lnk"}
EXEC_EXT = {".cmd", ".bat", ".rex", ".jar"}
MAX_FILES = 6000
MAX_DEPTH = 4
MAX_READ = 512 * 1024

KEYWORDS = re.compile(
    r"listennr|listen-?nr|berichtszeitraum|auswertung|berichtswesen|bwjava|\bbw\b|headless|batch|"
    r"automatik|nogui|-liste\b|/liste\b|ausgabedatei|outputfile|exportdatei",
    re.IGNORECASE,
)
# Never run anything that smells like changing data, backups or installs.
UNSAFE = re.compile(
    r"l(ö|oe)sch|delete|\bdel\b|rmdir|\brd\b|format|update|upgrade|import|buch|bestell|order|"
    r"sicherung|backup|restore|reorg|install|setup|uninstall|db2|\bsql\b|nachtjob|tagesabschluss|"
    r"kasse|inventur.*(buch|abschl)|preis.*(übern|uebern)|uebernahme|übernahme|reset|kill|taskkill|shutdown",
    re.IGNORECASE,
)
FLAG_LIST = re.compile(r"(?:^|\s)((?:-{1,2}|/)(?:liste|listennr|list|auswertung|report|nr))(?:[=:\s])", re.I)
FLAG_FROM = re.compile(r"(?:^|\s)((?:-{1,2}|/)(?:von|from|start|datumvon|vondatum|beginn))(?:[=:\s])", re.I)
FLAG_TO = re.compile(r"(?:^|\s)((?:-{1,2}|/)(?:bis|to|ende|datumbis|bisdatum|end))(?:[=:\s])", re.I)
FLAG_OUT = re.compile(r"(?:^|\s)((?:-{1,2}|/)(?:out|output|ausgabe|datei|file|export|ziel))(?:[=:\s])", re.I)

STATUS_TEXT = {
    "works": "Kommandozeile funktioniert – Listen werden ohne Bildschirmsteuerung erzeugt.",
    "degraded": "Kommandozeile hatte zuletzt Fehler – wird weiter versucht, sonst Bildschirmsteuerung.",
    "failed": "Kommandozeile funktioniert nicht – Listen werden per Bildschirmsteuerung erzeugt.",
    "not_found": "Keine Kommandozeile gefunden – Listen werden per Bildschirmsteuerung erzeugt (normal bei PROKAS).",
    "untested": "Kandidaten gefunden, aber noch nicht getestet (Test läuft beim nächsten erlaubten Zeitpunkt).",
    "disabled": "Kommandozeile in config.yaml abgeschaltet – nur Bildschirmsteuerung.",
    "unknown": "Noch nicht geprüft (passiert automatisch beim ersten Start des Connectors).",
}


class CliError(Exception):
    pass


@dataclass
class Candidate:
    path: str
    template: list[str]
    score: int
    reason: str

    def as_dict(self) -> dict:
        return {"path": self.path, "template": self.template, "score": self.score, "reason": self.reason}


def search_roots(cfg) -> list[Path]:
    roots: list[Path] = []
    if cfg.bw_install_dir and cfg.bw_install_dir.lower() != "auto":
        roots.append(Path(cfg.bw_install_dir))
    for d in cfg.prokas_search_dirs:
        roots.append(Path(d))
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen and r.exists():
            seen.add(key)
            out.append(r)
    return out


def detect_bw_install_dir(cfg) -> str | None:
    if cfg.bw_install_dir and cfg.bw_install_dir.lower() != "auto":
        return cfg.bw_install_dir
    for r in search_roots(cfg):
        for p in _walk(r, depth=2):
            if p.name.lower() in ("prostartx86.exe", "bwjava.jar", "berichtswesen.jar") or "berichtswesen" in p.name.lower():
                return str(p.parent)
    return None


def _walk(root: Path, depth: int = MAX_DEPTH):
    count = 0
    stack = [(root, 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            count += 1
            if count > MAX_FILES:
                return
            try:
                if e.is_dir(follow_symlinks=False):
                    if lvl < depth:
                        stack.append((Path(e.path), lvl + 1))
                elif e.is_file(follow_symlinks=False):
                    yield Path(e.path)
            except OSError:
                continue


def fingerprint(cfg) -> str:
    """Changes when PROKAS/Berichtswesen program files change (version update)."""
    h = hashlib.sha256()
    for r in search_roots(cfg):
        for p in sorted(_walk(r, depth=2), key=lambda x: str(x).lower()):
            if p.suffix.lower() in (".jar", ".exe", ".dll") or "version" in p.name.lower():
                try:
                    st = p.stat()
                except OSError:
                    continue
                h.update(f"{p.name}|{st.st_size}|{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def _read_text(p: Path) -> str:
    try:
        if p.stat().st_size > MAX_READ:
            return ""
        data = p.read_bytes()
    except OSError:
        return ""
    if p.suffix.lower() == ".jar":
        return ""  # binary; judged by name only
    return data.decode("cp1252", errors="ignore")


def _launcher(p: Path) -> list[str] | None:
    ext = p.suffix.lower()
    if ext in (".cmd", ".bat"):
        return ["cmd.exe", "/c", str(p)]
    if ext == ".rex":
        rexx = shutil.which("rexx") or shutil.which("regina")
        return [rexx, str(p)] if rexx else None
    if ext == ".jar":
        java = shutil.which("java") or shutil.which("javaw")
        return [java, "-jar", str(p)] if java else None
    return None


def discover(cfg) -> tuple[list[Candidate], list[dict], int]:
    """Read-only scan. Returns (candidates, skipped_unsafe, files_scanned)."""
    candidates: list[Candidate] = []
    skipped: list[dict] = []
    scanned = 0
    for root in search_roots(cfg):
        for p in _walk(root):
            if p.suffix.lower() not in SCAN_EXT:
                continue
            scanned += 1
            text = _read_text(p)
            hits = len(KEYWORDS.findall(text)) + len(KEYWORDS.findall(p.name))
            if hits == 0:
                continue
            if p.suffix.lower() not in EXEC_EXT:
                continue
            if UNSAFE.search(p.name) or UNSAFE.search(text):
                skipped.append({"path": str(p), "reason": "enthält Hinweise auf datenändernde Befehle"})
                continue
            flags = {name: (rx.search(text).group(1) if rx.search(text) else None)
                     for name, rx in (("list", FLAG_LIST), ("from", FLAG_FROM), ("to", FLAG_TO), ("out", FLAG_OUT))}
            if not (flags["list"] and flags["out"]):
                continue
            launcher = _launcher(p)
            if not launcher:
                skipped.append({"path": str(p), "reason": "kein Interpreter (rexx/java) gefunden"})
                continue
            tpl = launcher + [flags["list"], "{z_code}"]
            if flags["from"]:
                tpl += [flags["from"], "{date_from}"]
            if flags["to"]:
                tpl += [flags["to"], "{date_to}"]
            tpl += [flags["out"], "{output_file}"]
            candidates.append(Candidate(str(p), tpl, hits, f"Parameter gefunden: {', '.join(v for v in flags.values() if v)}"))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates, skipped, scanned


def fill_template(template: list[str], z_code: str, date_from: dt.date, date_to: dt.date, output_file: Path) -> list[str]:
    values = {"z_code": z_code, "date_from": date_from.strftime("%d.%m.%Y"),
              "date_to": date_to.strftime("%d.%m.%Y"), "output_file": str(output_file)}
    return [part.format(**values) for part in template]


def run_template(template: list[str], z_code: str, date_from: dt.date, date_to: dt.date,
                 output_file: Path, timeout_sec: float, cwd: Path) -> Path:
    """Run one headless export. Raises CliError on non-zero exit, timeout or missing/empty output."""
    for part in template:
        if UNSAFE.search(part) and "{" not in part:
            raise CliError(f"Befehlsteil {part!r} sieht nach einer datenändernden Aktion aus – nicht ausgeführt.")
    args = fill_template(template, z_code, date_from, date_to, output_file)
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(args, cwd=str(cwd), capture_output=True, timeout=timeout_sec,
                              stdin=subprocess.DEVNULL, shell=False)
    except subprocess.TimeoutExpired as e:
        raise CliError(f"Zeitüberschreitung nach {int(timeout_sec)} s") from e
    except OSError as e:
        raise CliError(f"Start fehlgeschlagen: {e}") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("cp1252", errors="ignore").strip()[:300]
        raise CliError(f"Exit-Code {proc.returncode}: {err}")
    if not output_file.exists():
        raise CliError("Keine Ausgabedatei erzeugt.")
    if output_file.stat().st_size == 0:
        raise CliError("Ausgabedatei ist leer.")
    return output_file


class CliStatusStore:
    def __init__(self, jobs_dir: Path):
        self.path = Path(jobs_dir) / "cli_status.json"

    def load(self) -> dict:
        import json

        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"status": "unknown"}

    def save(self, data: dict) -> dict:
        data = dict(data)
        data["status_text"] = STATUS_TEXT.get(data.get("status", "unknown"), "")
        write_json_atomic(self.path, data)
        return data
