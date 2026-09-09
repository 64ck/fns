"""Разбор XML-выгрузки открытых данных ФНС (ставки и льготы)."""
from __future__ import annotations

from fnsportal import analytics, autoload
from fnsportal.sources import fnsxml, rates

SAMPLE = """<?xml version="1.0" encoding="windows-1251"?>
<Файл ИдФайл="TAXRATES">
 <List>
  <li ID="2601" List_value="77 - город Москва"/>
  <li ID="2602" List_value="50 - Московская область"/>
  <li ID="2802" List_value="Транспортный налог"/>
  <li ID="2803" List_value="Земельный налог"/>
 </List>
 <tp ID="A1" Region_ID="2601" Nalog_ID="2802" TaxPeriod="2023" MunObraz="г. Москва"
     Oktmo_ID="45000000" LawDoc="Закон г. Москвы &quot;О транспортном налоге&quot;"
     LawNum="33" LawDate="09.07.2008">
   <tr TaxObject="Автомобили легковые (с каждой лошадиной силы): до 100 л.с. включительно"
       TaxRates="12" Fl="1" UL="1" IP="0"/>
   <tb Category="Пенсионеры, получающие страховую пенсию по старости" Amount="100" Unit="%"
       Condition="в отношении одного транспортного средства" Base="п. 1 ст. 4" Fl="1" UL="0" IP="0"/>
   <tb Category="Организации, осуществляющие перевозки пассажиров" Amount="50" Unit="%"
       Fl="0" UL="1" IP="1"/>
 </tp>
 <tp ID="A2" Region_ID="2602" Nalog_ID="2803" TaxPeriod="2022" Oktmo_ID="46000000"
     LawDoc="Решение Совета"/>
</Файл>"""


def write_sample(path):
    path.write_bytes(SAMPLE.encode("cp1251"))
    return path


def test_reader_understands_structure_and_encoding(tmp_path):
    path = write_sample(tmp_path / "data.xml")
    assert fnsxml.looks_like_export(path)
    assert fnsxml.read_list_values(path)["2601"] == "77 - город Москва"

    records = list(fnsxml.iter_records(path))
    assert [r.attrs["ID"] for r in records] == ["A1", "A2"]
    first = records[0]
    assert first.attrs["LawDoc"] == 'Закон г. Москвы "О транспортном налоге"'
    assert len(first.rates) == 1 and len(first.benefits) == 2
    assert fnsxml.payer_flags(first.benefits[0]) == {"fl": True, "ul": False, "ip": False}
    assert fnsxml.payer_flags(first.benefits[1]) == {"fl": False, "ul": True, "ip": True}
    assert records[1].rates == [] and records[1].benefits == []


def test_object_group_splits_limit_from_group():
    group, detail = fnsxml.object_group("Гидроциклы (с каждой л.с.):  свыше 100 л.с. ")
    assert group == "Гидроциклы (с каждой л.с.)"
    assert detail == "свыше 100 л.с."
    assert fnsxml.object_group("Прочие") == ("Прочие", "")


def test_records_split_across_read_chunks(tmp_path, monkeypatch):
    """Запись не должна теряться, если попала на границу куска чтения."""
    path = write_sample(tmp_path / "data.xml")
    monkeypatch.setattr(fnsxml, "CHUNK", 64)
    records = list(fnsxml.iter_records(path))
    assert [r.attrs["ID"] for r in records] == ["A1", "A2"]
    assert len(records[0].benefits) == 2


def test_loader_uses_payer_flags_from_export(conn, tmp_path):
    path = write_sample(tmp_path / "data.xml")
    stats = rates.load_fns_xml(conn, path)
    assert (stats.rows_read, stats.rates, stats.benefits) == (2, 1, 2)

    rate = conn.execute("SELECT * FROM rate").fetchone()
    assert rate["region_code"] == "77" and rate["tax_code"] == "tn" and rate["year"] == 2023
    assert rate["rate_value"] == 12.0
    assert rate["object_group"] == "Автомобили легковые (с каждой лошадиной силы)"
    assert (rate["for_fl"], rate["for_ul"], rate["for_ip"]) == (1, 1, 0)

    # категории берутся из выгрузки, а не угадываются по тексту
    fl = analytics.benefits(conn, "tn", 2023, "77", payer="fl")
    ul = analytics.benefits(conn, "tn", 2023, "77", payer="ul")
    ip = analytics.benefits(conn, "tn", 2023, "77", payer="ip")
    assert fl["total"] == 1 and fl["items"][0]["category"].startswith("Пенсионеры")
    assert ul["total"] == 1 and ip["total"] == 1
    assert ul["items"][0]["id"] == ip["items"][0]["id"]      # одна льгота в двух категориях
    assert analytics.benefit_counts(conn, "tn", 2023, "77")["total"] == 2

    empty = conn.execute("SELECT COUNT(*) FROM rate WHERE tax_code='zn'").fetchone()[0]
    assert empty == 0            # у документа A2 нет ни ставок, ни льгот


def test_autoload_recognises_export(conn, tmp_path):
    write_sample(tmp_path / "8645516166454040bb4e3920a224069a.xml")
    result = autoload.run(conn, tmp_path, log=lambda _: None)
    assert [kind for _name, kind, _summary in result.imported] == ["rates-xml"]
    terms = analytics.wordcloud(conn, "tn", "ul", years_selected=[2023], limit=10)
    assert any("организации" in item["text"] for item in terms["items"])
