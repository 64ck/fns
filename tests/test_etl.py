import csv

import openpyxl

from fnsportal import config
from fnsportal.sources import forms, methodology, rates


def test_methodology_slugs_are_stable_across_years():
    meth = methodology.read_map_csv(config.REFERENCE_DIR / "form_5tn_map.csv")
    years = {}
    for row in meth.rows:
        years.setdefault(row.slug, set()).add(row.year)
    # верхнеуровневые показатели должны быть сквозными за все 20 лет методички
    assert len(meth.years) == 20
    full = [slug for slug, ys in years.items() if len(ys) == len(meth.years)]
    assert len(full) >= 14
    assert any("количество-налогоплательщиков" in slug for slug in full)
    assert any(slug.startswith("s4--по-водным") for slug in full)


def test_methodology_loaded_into_db(conn):
    codes = conn.execute(
        "SELECT COUNT(*) FROM indicator_code WHERE tax_code='tn'").fetchone()[0]
    assert codes > 2000
    # у формы 2006 года заданы графы для ЮЛ и ФЛ
    row = conn.execute(
        "SELECT col_index FROM indicator_code"
        " WHERE year=2006 AND payer='fl' AND row_code='010'").fetchone()
    assert row["col_index"] == 2


def _rates_csv(path):
    header = ["Код региона", "Наименование региона", "ОКТМО", "Вид налога",
              "Налоговый период", "Объект налогообложения", "Размер ставки",
              "Плательщик", "Категория налогоплательщика", "Вид льготы",
              "Размер льготы", "Условие предоставления", "Основание",
              "Наименование НПА", "Дата начала действия", "Дата окончания действия"]
    rows = [
        ["77", "город Москва", "45000000", "Транспортный налог", "2023",
         "Автомобили легковые до 100 л.с.", "12", "Физические лица", "", "", "", "", "",
         "Закон г. Москвы", "01.01.2023", ""],
        ["", "Московская область", "46000000", "Транспортный налог", "2023", "", "", "",
         "Многодетные родители, имеющие трёх детей", "Освобождение", "100",
         "в отношении одного ТС", "ст. 25", "Закон МО", "01.01.2023", ""],
        ["16", "Республика Татарстан", "92000000", "Земельный налог", "", "Земли сельхозназначения",
         "0,3", "Организации", "Организации инвалидов", "Пониженная ставка", "50", "", "п.2 ст.3",
         "Решение Совета", "01.01.2020", "31.12.2024"],
    ]
    with path.open("w", encoding="cp1251", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(header)
        writer.writerows(rows)
    return path


def test_rates_etl_splits_rates_and_benefits(conn, tmp_path):
    stats = rates.load_file(conn, _rates_csv(tmp_path / "rates.csv"))
    assert stats.rows_read == 3
    assert (stats.rates, stats.benefits) == (2, 2)

    # регион определён по названию, когда код пустой
    benefit = conn.execute(
        "SELECT * FROM benefit WHERE region_code='50'").fetchone()
    assert benefit["payer"] == "fl"
    assert benefit["size_value"] == 100.0

    # период действия разложен на границы годов
    land = conn.execute("SELECT * FROM rate WHERE tax_code='zn'").fetchone()
    assert (land["year_from"], land["year_to"]) == (2020, 2024)
    assert land["payer"] == "ul"


def test_rates_etl_learns_oktmo_prefix(conn, tmp_path):
    path = tmp_path / "r.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["Код региона", "Наименование региона", "ОКТМО", "Вид налога",
                         "Налоговый период", "Объект налогообложения", "Размер ставки"])
        writer.writerow(["77", "город Москва", "45301000", "Транспортный налог", "2023", "Легковые", "12"])
        writer.writerow(["", "", "45302000", "Транспортный налог", "2023", "Грузовые", "40"])
    rates.load_file(conn, path)
    codes = [row["region_code"] for row in conn.execute("SELECT region_code FROM rate")]
    assert codes == ["77", "77"]


def _form_xlsx(path, rows, title):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([title])
    ws.append(["Показатель", "Код строки", "Значение", "Значение 2"])
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_forms_etl_new_layout(conn, tmp_path):
    path = _form_xlsx(tmp_path / "5tn_2019_50.xlsx",
                      [["НП ЮЛ", "1100", 100], ["НП ФЛ", "2100", 900],
                       ["Сумма ЮЛ", "1400", 5000], ["Сумма ФЛ", "2400", 7000]],
                      "Отчёт по форме 5-ТН")
    stats = forms.load_file(conn, path)
    assert stats.problems == []
    assert (stats.year, stats.region_code) == (2019, "50")
    assert stats.values == 4
    forms.recompute_totals(conn, "tn")
    total = conn.execute(
        """SELECT value FROM fact_form f JOIN indicator i ON i.id=f.indicator_id
            WHERE f.payer='total' AND i.section='1' AND f.year=2019""").fetchone()
    assert total["value"] == 1000


def test_forms_etl_old_layout_uses_columns(conn, tmp_path):
    path = _form_xlsx(tmp_path / "form.xlsx",
                      [["Количество налогоплательщиков", "010", 999, 88888],
                       ["Сумма налога", "120", 30000, 45000]],
                      "Отчёт за 2006 год")
    stats = forms.load_file(conn, path, year=2006, region_code="16")
    assert stats.problems == []
    values = {
        (row["payer"], row["section"]): row["value"]
        for row in conn.execute(
            """SELECT f.payer, i.section, f.value FROM fact_form f
                 JOIN indicator i ON i.id = f.indicator_id WHERE f.year=2006""")
    }
    assert values[("ul", "1")] == 999      # графа 1 — юрлица
    assert values[("fl", "1")] == 88888    # графа 2 — физлица
    assert values[("fl", "4")] == 45000


def test_forms_reports_unknown_year(conn, tmp_path):
    path = _form_xlsx(tmp_path / "noyear.xlsx", [["НП", "1100", 1]], "Отчёт")
    stats = forms.load_file(conn, path, region_code="77")
    assert any("год" in problem for problem in stats.problems)


def test_column_mapping_prefers_specific_alias():
    """«Категория налогоплательщика» — это льготная категория, а не колонка «Плательщик»."""
    aliases = rates.load_aliases()
    header = ["Код региона", "Вид налога", "Налоговый период", "Объект налогообложения",
              "Размер ставки", "Категория налогоплательщика", "Вид льготы", "Размер льготы"]
    mapping = rates.map_columns(header, aliases)
    assert mapping["benefit_category"] == "Категория налогоплательщика"
    assert "payer_text" not in mapping

    with_payer = rates.map_columns(header + ["Плательщик"], aliases)
    assert with_payer["payer_text"] == "Плательщик"
    assert with_payer["benefit_category"] == "Категория налогоплательщика"


def test_autoload_classifies_and_skips_repeats(conn, tmp_path):
    from fnsportal import autoload

    _rates_csv(tmp_path / "taxrates.csv")
    _form_xlsx(tmp_path / "5tn_2019_50.xlsx", [["НП ЮЛ", "1100", 10], ["НП ФЛ", "2100", 90],
                                               ["ТС ЮЛ", "1200", 15], ["Сумма ЮЛ", "1400", 50]],
               "Отчёт по форме 5-ТН")
    (tmp_path / "заметки.csv").write_text("просто;текст\n1;2\n", encoding="utf-8")

    first = autoload.run(conn, tmp_path, log=lambda _: None)
    kinds = {name: kind for name, kind, _ in first.imported}
    assert kinds == {"taxrates.csv": "rates", "5tn_2019_50.xlsx": "forms"}
    assert first.unknown == ["заметки.csv"]
    assert conn.execute("SELECT COUNT(*) FROM benefit_term").fetchone()[0] > 0

    second = autoload.run(conn, tmp_path, log=lambda _: None)
    assert second.imported == [] and len(second.skipped) == 2
