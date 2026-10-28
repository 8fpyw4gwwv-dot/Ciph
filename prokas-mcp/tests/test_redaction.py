import json

import pytest
import yaml

from fixture_data import PERSON, PERSON_MAIL, write_txt
from prokas_mcp.app import App
from prokas_mcp.config import DEFAULTS, PROJECT_ROOT
from prokas_mcp.mapping import load_mapping_file
from prokas_mcp.parser import parse_export
from prokas_mcp.redaction import Redactor

RED = Redactor(DEFAULTS["denylist_patterns"])


@pytest.mark.parametrize("header", [
    "Name", "Vorname", "Kunde", "Kundennr.", "Kundenname", "Stammkunde", "Geb.-Datum", "Geburtsdatum", "Adresse",
    "Straße", "PLZ", "Telefon", "Tel.", "E-Mail", "Mail", "Versichertennummer", "Versicherten-Nr", "Arzt",
    "Arztname", "Rezept-ID", "RezeptID", "Patient", "Patientenname", "Name Kunde", "Kunden-Name", "IBAN", "Bearbeiter",
])
def test_personal_headers_detected(header):
    assert RED.is_personal(header)


@pytest.mark.parametrize("header", ["PZN", "Artikelbezeichnung", "Artikelname", "Listen-EK", "Bestand", "Spanne %",
                                    "Lieferant", "Mahngebühr", "Lagerort", "Rechnungsdatum", "Umsatz"])
def test_business_headers_kept(header):
    assert not RED.is_personal(header)


def test_scrub_values():
    v, changed = RED.scrub(f"Rückfrage an {PERSON_MAIL} bzw. DE89 3704 0044 0532 0130 00")
    assert changed and PERSON_MAIL not in v and "3704" not in v


def test_denylist_beats_mapping(tmp_path):
    """Even if someone adds a personal column to the mapping, it never leaves the server."""
    src = yaml.safe_load((PROJECT_ROOT / "mappings" / "Z0705.yaml").read_text(encoding="utf-8"))
    src["columns"]["kunde_name"] = {"aliases": ["Name"], "type": "text"}
    src["columns"]["kundennr"] = {"aliases": ["Kunde"], "type": "text"}
    p = tmp_path / "Z0705.yaml"
    p.write_text(yaml.safe_dump(src, allow_unicode=True), encoding="utf-8")
    mapping = load_mapping_file(p)
    parsed = parse_export(write_txt("Z0705", tmp_path), mapping, RED)
    assert "kunde_name" not in parsed.fields and "kundennr" not in parsed.fields
    assert {"Name", "Kunde", "E-Mail"} <= set(parsed.redactions.personal_columns)
    assert "Rechnungsnummer" in parsed.redactions.unmapped_columns
    blob = json.dumps([{k: str(v) for k, v in r.items()} for r in parsed.records], ensure_ascii=False)
    assert PERSON not in blob and "10001" not in blob and "R-2026" not in blob


def test_mandatory_denylist_survives_config(tmp_path):
    from conftest import make_cfg

    cfg = make_cfg(tmp_path, denylist_patterns=["Foo"])
    r = Redactor(cfg.denylist_patterns)
    for h in ("Name", "Kunde", "Patient", "Arzt", "Geburtsdatum", "Rezept-ID"):
        assert r.is_personal(h)


def test_tools_never_return_personal_data_and_audit_has_no_values(app, fresh):
    for z in ("Z0015", "Z0016", "Z0705", "Z0317"):
        write_txt(z, app.cfg.export_dir, f"{z}_nacht.txt", mtime=fresh)
    from prokas_mcp.server import build_server
    import asyncio

    mcp, _ = build_server(app.cfg, app)
    outputs = []
    for name, args in [("run_report", {"z_code": "Z0015"}), ("run_report", {"z_code": "Z0705"}),
                       ("run_report", {"z_code": "Z0317"}), ("lost_sales", {}), ("open_items", {}),
                       ("validate_mapping", {"z_code": "Z0705"})]:
        res = asyncio.run(mcp.call_tool(name, args))
        outputs.append(json.dumps(res.model_dump(), ensure_ascii=False, default=str))
    blob = "\n".join(outputs)
    assert PERSON not in blob and PERSON_MAIL not in blob
    assert "R-2026" not in blob  # invoice numbers stay inside
    audit = "".join(p.read_text(encoding="utf-8") for p in app.cfg.log_dir.glob("audit-*.jsonl"))
    lines = [json.loads(x) for x in audit.splitlines()]
    assert len(lines) == 6
    assert PERSON not in audit and "Ibuprofen" not in audit
    z705 = next(e for e in lines if e["params"].get("z_code") == "Z0705" and e["tool"] == "run_report")
    assert "Name" in z705["redactions"]["personal_columns_removed"]
    assert z705["mode"] == "file" and z705["row_count"] == 3 and z705["files"]
    # validate_mapping proposals in the outbox carry column names, never values.
    for f in app.cfg.outbox_dir.glob("*"):
        assert PERSON not in f.read_text(encoding="utf-8")
