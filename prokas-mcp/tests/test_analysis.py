import datetime as dt
import json

import pytest

from fixture_data import P, PERSON, write_txt, write_xlsx
from prokas_mcp.analysis import Analyses
from prokas_mcp.app import UserError


@pytest.fixture
def an(app):
    return Analyses(app)


def put(app, fresh, *codes, xlsx=False):
    for z in codes:
        if xlsx:
            write_xlsx(z, app.cfg.export_dir, f"{z}_nacht.xlsx", mtime=fresh)
        else:
            write_txt(z, app.cfg.export_dir, f"{z}_nacht.txt", mtime=fresh)


def test_margin_check_suggestions(app, an, fresh):
    put(app, fresh, "Z0125")
    r = an.margin_check(threshold_pct=20)
    assert r["status"] == "ok" and r["mode"] == "file"
    by = {i["pzn"]: i for i in r["items"]}
    assert P[9] not in by  # Rx excluded
    assert "verschreibungspflichtig" in " ".join(r["warnings"])
    ibu = by[P[0]]
    assert ibu["spanne_pct"] == 14.8
    # EK 2,50 / (1-0,20) * 1,19 = 3,71875 -> rounded up to 3,75
    assert ibu["vk_vorschlag"] == 3.75 and ibu["vk_differenz"] == 0.26
    nasal = by[P[1]]  # no Spanne column value -> computed: net 2,95/1,19=2,479; (2,479-2,10)/2,479 = 15,3 %
    assert nasal["spanne_pct"] == pytest.approx(15.3, abs=0.1)
    assert {"pzn": P[0], "price": 3.75} in r["price_file_items"]
    assert any("Z0585" in w for w in r["warnings"])  # optional list missing -> noted, not generated
    assert app.jobs.all() == []


def test_margin_check_target_and_optional_lists(app, an, fresh):
    put(app, fresh, "Z0125", "Z0585", "Z0165")
    r = an.margin_check(threshold_pct=20, target_margin_pct=30)
    assert {s["z_code"] for s in r["sources"]} == {"Z0125", "Z0585", "Z0165"}
    assert r["rohertrag_overview"]["rohertrag_pct"] == pytest.approx(26.7, abs=0.1)
    by = {i["pzn"]: i for i in r["items"]}
    assert by[P[0]]["vk_vorschlag"] == 4.25  # 2,50/0,7*1,19 = 4,25
    assert P[10] not in by  # 26,5 % >= 20 %


def test_missing_required_list_passes_through_needs_computer_use(app, an):
    r = an.lost_sales(period="letzte 7 Tage")
    assert r["status"] == "needs_computer_use"
    assert r["pending"][0]["z_code"] == "Z0015" and r["pending"][1]["status"] == "queued"
    assert "runbook" in r and r["job_id"] in r["runbook"]
    assert "lost_sales" in r["message"]


def test_lost_sales(app, an, fresh):
    put(app, fresh, "Z0015", "Z0016")
    r = an.lost_sales()
    top = r["items"][0]
    assert top["pzn"] == P[1] and top["neinverkauf_menge"] == 3 and top["nachfragen_menge"] == 3
    ibu = next(i for i in r["items"] if i["pzn"] == P[0])
    assert ibu["nachfragen_anzahl"] == 2 and ibu["nachfragen_menge"] == 3
    assert r["totals"]["neinverkauf_packungen"] == 4
    assert PERSON not in json.dumps(r, ensure_ascii=False, default=str)


def test_defect_rate(app, an, fresh):
    put(app, fresh, "Z0245")
    r = an.defect_rate()
    assert r["defektquote_pct"] == 2.0  # 400 / 20000
    assert r["lieferfaehigkeit_pct"] == 98.0
    assert "2,0 %" in r["message"]


def test_expiry_risk(app, an, fresh):
    put(app, fresh, "Z0051", "Z0342")
    r = an.expiry_risk(months=3)
    pzns = [i["pzn"] for i in r["items"]]
    assert pzns[0] == P[6] and r["already_expired"] == 1
    assert P[5] in pzns  # 15.01.2027 within 3 months of 27.10.2026
    insulin = next(i for i in r["items"] if i["pzn"] == P[4])
    assert insulin["wert_ek"] == 160.0
    assert len(r["without_date"]) == 1
    assert an.expiry_risk(months=1)["items"].__len__() < len(r["items"])


def test_slow_movers(app, an, fresh):
    put(app, fresh, "Z0127", xlsx=True)
    r = an.slow_movers()
    assert [i["pzn"] for i in r["items"]] == [P[10], P[4], P[2]]  # by Lagerwert
    assert r["items"][2].get("letzter_verkauf") is None  # never sold
    r = an.slow_movers(days=100)
    assert {i["pzn"] for i in r["items"]} == {P[4], P[2]}
    r = an.slow_movers(min_ek=56)
    assert {i["pzn"] for i in r["items"]} == {P[4], P[2]}


def test_open_returns(app, an, fresh):
    put(app, fresh, "Z0207")
    r = an.open_returns()
    gehe = next(s for s in r["by_supplier"] if s["lieferant"] == "Gehe")
    assert gehe["positionen"] == 2 and gehe["wert"] == 14.5 and gehe["aelteste_retoure"] == "2026-10-01"


def test_open_items_has_no_personal_data(app, an, fresh):
    put(app, fresh, "Z0705")
    r = an.open_items()
    assert r["count"] == 3 and r["total_open"] == 440.0
    aging = {a["ueberfaellig"]: a for a in r["aging"]}
    assert aging["> 90 Tage"]["betrag"] == 120.0 and aging["nicht fällig"]["betrag"] == 20.0
    blob = json.dumps(r, ensure_ascii=False, default=str)
    assert PERSON not in blob and "R-2026" not in blob and "10001" not in blob and "@" not in blob


def test_inventory_data_quality(app, an, fresh):
    put(app, fresh, "Z0327", "Z0117")
    r = an.inventory_data_quality()
    checks = {c["z_code"]: c for c in r["checks"]}
    assert checks["Z0327"]["count"] == 2 and checks["Z0327"]["mit_bestand"] == 1
    assert checks["Z0117"]["vek_differenz_summe"] == 0.5
    assert checks["Z0134"]["available"] is False


def test_kpi_snapshot(app, an, fresh):
    put(app, fresh, "Z0003", "Z0165", "Z0105", "Z0245")
    r = an.kpi_snapshot()
    k = r["kpi"]
    assert k["umsatz_netto"] == 75000.0
    assert k["rohertrag_pct"] == pytest.approx(26.7, abs=0.1)  # Z0165: 20.000 / 75.000
    assert k["lagerumschlag"] == 9.0  # 450.000 / 50.000
    assert k["defektquote_pct"] == 2.0
    assert k["umsatz_vs_vorjahr_pct"] == pytest.approx(5.6, abs=0.1)
    assert r["message"].startswith("Umsatz netto 75.000 €")


def test_kpi_snapshot_explicit_period_needs_generation(app, an, fresh):
    put(app, fresh, "Z0003", "Z0165", "Z0105")
    r = an.kpi_snapshot(period="letzter Monat")
    assert r["status"] == "needs_computer_use"
    assert [p["z_code"] for p in r["pending"]] == ["Z0003", "Z0165", "Z0105"]


def test_get_article_merges_lists(app, an, fresh):
    put(app, fresh, "Z0125", "Z0015", "Z0713", "Z0051")
    r = an.get_article(P[0][1:] if P[0].startswith("0") else P[0])
    a = r["article"]
    assert a["bezeichnung"].startswith("Ibuprofen") and a["ek"] == 2.5 and a["vk"] == 3.49
    assert a["amo"] == 8 and a["mb"] == 4
    meanings = {f["z_code"]: f["meaning"] for f in r["appears_in"]}
    assert "Spannenkiller" in meanings["Z0125"] and "2×" in meanings["Z0015"]
    assert PERSON not in json.dumps(r, ensure_ascii=False, default=str)


def test_get_article_validation(an):
    with pytest.raises(UserError):
        an.get_article("1234")
    assert an.get_article(P[3])["status"] == "not_found"
