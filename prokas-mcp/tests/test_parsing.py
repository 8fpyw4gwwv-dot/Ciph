import datetime as dt
import json

import pytest

from fixture_data import FIXTURES, P, PERSON, TOTALS, write_txt, write_xlsx
from prokas_mcp.config import PROJECT_ROOT
from prokas_mcp.mapping import load_mappings
from prokas_mcp.parser import ParseError, parse_export, records_for_output
from prokas_mcp.readers import decode_bytes, detect_separator, read_raw
from prokas_mcp.redaction import Redactor
from prokas_mcp.config import DEFAULTS

MAPPINGS = load_mappings(PROJECT_ROOT / "mappings")
RED = Redactor(DEFAULTS["denylist_patterns"])


def test_every_allowed_list_has_mapping_and_fixture():
    for z in DEFAULTS["allowed_z_codes"]:
        assert z in MAPPINGS, z
        assert z in FIXTURES, z


@pytest.mark.parametrize("z", sorted(FIXTURES))
@pytest.mark.parametrize("fmt", ["txt", "xlsx"])
def test_each_list_parses(tmp_path, z, fmt):
    path = write_txt(z, tmp_path) if fmt == "txt" else write_xlsx(z, tmp_path)
    parsed = parse_export(path, MAPPINGS[z], RED)
    headers, rows = FIXTURES[z]
    assert parsed.row_count == len(rows), parsed.dropped_rows
    if z in TOTALS:
        assert parsed.dropped_rows["footer"] + parsed.dropped_rows["no_key"] >= 1
    for f, spec in MAPPINGS[z].fields.items():
        if spec.required:
            assert f in parsed.fields
    # No personal data anywhere in what would be returned.
    out, _ = records_for_output(parsed, 500)
    blob = json.dumps(out, ensure_ascii=False)
    assert PERSON not in blob
    assert "@" not in blob
    for h in parsed.redactions.personal_columns:
        assert h not in parsed.fields


def test_txt_cp1252_tab_with_preamble_and_period(tmp_path):
    path = write_txt("Z0015", tmp_path, period=("01.10.2026", "27.10.2026"))
    raw = read_raw(path)
    assert raw.encoding == "windows-1252"
    assert raw.separator == "\t"
    parsed = parse_export(path, MAPPINGS["Z0015"], RED)
    assert parsed.period == (dt.date(2026, 10, 1), dt.date(2026, 10, 27))
    assert parsed.header_row == 5
    assert parsed.records[0]["datum"] == dt.date(2026, 10, 20)
    assert parsed.records[0]["menge"] == 2.0
    assert "Kunde" in parsed.redactions.personal_columns
    assert "Bearbeiter" in parsed.redactions.personal_columns


def test_utf8_and_semicolon(tmp_path):
    path = write_txt("Z0125", tmp_path, encoding="utf-8", sep=";")
    raw = read_raw(path)
    assert raw.encoding == "utf-8" and raw.separator == ";"
    parsed = parse_export(path, MAPPINGS["Z0125"], RED)
    assert parsed.records[0]["ek"] == 2.5 and parsed.records[0]["vk"] == 3.49
    assert parsed.records[0]["spanne"] == pytest.approx(14.8)


def test_page_break_repeated_header_and_footer(tmp_path):
    path = write_txt("Z0051", tmp_path, page_break=True)
    parsed = parse_export(path, MAPPINGS["Z0051"], RED)
    assert parsed.row_count == 4
    assert parsed.dropped_rows["repeated_header"] == 1
    assert parsed.dropped_rows["footer"] >= 2  # "Seite 2" and "Summe"
    by_pzn = {r["pzn"]: r for r in parsed.records}
    assert by_pzn[P[4]]["verfall"] == dt.date(2026, 12, 31)  # "12/2026" -> end of month
    assert by_pzn[P[3]]["verfall"] == dt.date(2026, 11, 30)


def test_xlsx_native_values_and_leading_zero_pzn(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Ladenhüter"])
    ws.append(["PZN", "Artikelbezeichnung", "Listen-EK", "Letzter Verkauf", "Bestand"])
    ws.append([4908802, "Altes Produkt", 51.2, dt.datetime(2026, 7, 1), 2])
    ws.append(["Summe", None, None, None, 2])
    p = tmp_path / "Z0127_x.xlsx"
    wb.save(p)
    parsed = parse_export(p, MAPPINGS["Z0127"], RED)
    assert parsed.row_count == 1
    r = parsed.records[0]
    assert r["pzn"] == "04908802"
    assert r["ek"] == 51.2 and r["letzter_verkauf"] == dt.date(2026, 7, 1)


def test_fixed_width_text(tmp_path):
    lines = ["Liste Z0342", "", "PZN        Artikelbezeichnung      Bestand   Lagerort",
             f"{P[9]}   Sirup 200 ml            3         Sichtwahl", "Summe                              3"]
    p = tmp_path / "Z0342_fixed.txt"
    p.write_bytes("\r\n".join(lines).encode("cp1252"))
    parsed = parse_export(p, MAPPINGS["Z0342"], RED)
    assert read_raw(p).separator == "fixed"
    assert parsed.row_count == 1 and parsed.records[0]["bestand"] == 3


def test_wrong_list_in_file_is_rejected(tmp_path):
    path = write_txt("Z0015", tmp_path)
    with pytest.raises(ParseError):
        parse_export(path, MAPPINGS["Z0705"], RED)


def test_preamble_z_code_mismatch(tmp_path):
    path = write_txt("Z0016", tmp_path)
    text = path.read_bytes().replace(b"Liste Z0016", b"Liste Z0015")
    path.write_bytes(text)
    with pytest.raises(ParseError, match="Z0015"):
        parse_export(path, MAPPINGS["Z0016"], RED)


def test_xls_that_is_really_text(tmp_path):
    src = write_txt("Z0322", tmp_path)
    p = tmp_path / "Z0322.xls"
    p.write_bytes(src.read_bytes())
    parsed = parse_export(p, MAPPINGS["Z0322"], RED)
    assert parsed.row_count == 1
    assert any("Text" in n for n in parsed.format.get("notes", []))


def test_decode_and_separator_helpers():
    assert decode_bytes("Größe".encode("cp1252")) == ("Größe", "windows-1252")
    assert decode_bytes("﻿Größe".encode("utf-8"))[1] == "utf-8-sig"
    assert detect_separator(["a;b;c", "1,5;2,5;3"]) == ";"
    assert detect_separator(["a\tb", "1,5\t2"]) == "\t"
    assert detect_separator(["Name    Wert", "x       1"]) == "fixed"


def test_empty_file(tmp_path):
    from prokas_mcp.readers import ReadError

    p = tmp_path / "Z0003.txt"
    p.write_bytes(b"")
    with pytest.raises(ReadError):
        read_raw(p)


FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"


@pytest.mark.parametrize("path", sorted(FIXTURE_DIR.glob("Z*")), ids=lambda p: p.name)
def test_committed_fixture_files(path):
    z = path.name[:5]
    parsed = parse_export(path, MAPPINGS[z], RED)
    assert parsed.row_count == len(FIXTURES[z][1])
    assert PERSON not in json.dumps(records_for_output(parsed, 500)[0], ensure_ascii=False)
