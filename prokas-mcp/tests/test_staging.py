import asyncio
import json

import pytest

from fixture_data import P, write_txt
from prokas_mcp.server import build_server


def call(mcp, name, args):
    res = asyncio.run(mcp.call_tool(name, args))
    return res.structured_content or json.loads(res.content[0].text)


@pytest.fixture
def mcp(app):
    return build_server(app.cfg, app)[0]


def test_price_file_format_and_preview(app, mcp, fresh):
    write_txt("Z0125", app.cfg.export_dir, "Z0125_nacht.txt", mtime=fresh)
    r = call(mcp, "stage_price_file", {"items": [{"pzn": P[0], "price": 3.75}, {"pzn": P[1], "price": "2,99"}],
                                       "valid_from": "28.10.2026", "valid_to": "31.12.2026", "label": "Spannen Okt"})
    assert r["status"] == "ok", r
    csv = next(app.cfg.outbox_dir.glob("*.csv"))
    md = next(app.cfg.outbox_dir.glob("*.md"))
    raw = csv.read_bytes()
    assert raw == f"PZN;Preis\r\n{P[0]};3,75\r\n{P[1]};2,99\r\n".encode("cp1252")
    text = md.read_text(encoding="utf-8")
    assert "3,49" in text and "+0,26" in text and "28.10.2026" in text and "Nichts wurde in PROKAS geändert" in text
    assert csv.parent == app.cfg.outbox_dir


def test_price_file_never_overwrites(app, mcp):
    args = {"items": [{"pzn": P[0], "price": 3.75}], "valid_from": "28.10.2026", "label": "x"}
    call(mcp, "stage_price_file", args)
    call(mcp, "stage_price_file", args)
    assert len(list(app.cfg.outbox_dir.glob("*.csv"))) == 2
    assert len(list(app.cfg.outbox_dir.glob("*.md"))) == 2


@pytest.mark.parametrize("items,msg", [
    ([{"pzn": "12345670", "price": 3}], "Prüfziffer"),  # 12345678 would be valid
    ([{"pzn": "123", "price": 3}], "Ziffern"),
    ([{"pzn": P[0], "price": 0}], "größer als 0"),
    ([{"pzn": P[0], "price": -1}], "größer als 0"),
    ([{"pzn": P[0], "price": "abc"}], "keine Zahl"),
    ([{"pzn": P[0], "price": 1}, {"pzn": P[0], "price": 2}], "doppelt"),
    ([], "Keine Artikel"),
])
def test_price_file_validation(app, mcp, items, msg):
    r = call(mcp, "stage_price_file", {"items": items, "valid_from": "28.10.2026"})
    assert r["status"] == "error" and msg in r["message"]
    assert list(app.cfg.outbox_dir.glob("*")) == []


def test_price_file_label_cannot_escape_outbox(app, mcp, tmp_path):
    r = call(mcp, "stage_price_file", {"items": [{"pzn": P[0], "price": 3}], "valid_from": "28.10.2026",
                                       "label": "..\\..\\Windows\\evil"})
    assert r["status"] == "ok"
    files = list(app.cfg.outbox_dir.iterdir())
    assert len(files) == 2 and all(f.parent == app.cfg.outbox_dir for f in files)


def test_order_list(app, mcp):
    r = call(mcp, "stage_order_list", {"supplier": "Gehe", "items": [{"pzn": P[0], "qty": 2}, {"pzn": P[0], "qty": 1},
                                                                    {"pzn": P[1], "qty": "5"}]})
    assert r["status"] == "ok"
    csv = next(app.cfg.outbox_dir.glob("Bestellung_Gehe_*.csv"))
    assert csv.read_bytes() == f"PZN;Menge\r\n{P[0]};3\r\n{P[1]};5\r\n".encode("cp1252")
    assert "nichts bestellt" in r["message"]
    bad = call(mcp, "stage_order_list", {"supplier": "Gehe", "items": [{"pzn": P[0], "qty": 1.5}]})
    assert bad["status"] == "error"
