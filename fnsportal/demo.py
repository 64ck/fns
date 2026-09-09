"""Генератор демонстрационного набора данных.

Нужен, чтобы портал можно было запустить и проверить до того, как выгрузки
ФНС окажутся на диске (файлы ставок и льгот весят около 3 ГБ и качаются долго).
Структура данных полностью совпадает с боевой: те же показатели формы 5-ТН из
методички, те же категории плательщиков, те же формулировки льгот.

ВАЖНО: значения синтетические. Реальные данные загружаются командами
load-rates / load-forms и полностью замещают демонстрационные.
"""
from __future__ import annotations

import random
import sqlite3
from typing import Sequence

from . import config
from .sources.rates import flags_for
from .textutil import detect_payer

RATE_OBJECTS = [
    ("Автомобили легковые с мощностью двигателя до 100 л.с. (до 73,55 кВт) включительно", 12, 2.5),
    ("Автомобили легковые с мощностью двигателя свыше 100 л.с. до 150 л.с. включительно", 25, 5),
    ("Автомобили легковые с мощностью двигателя свыше 150 л.с. до 200 л.с. включительно", 45, 10),
    ("Автомобили легковые с мощностью двигателя свыше 200 л.с. до 250 л.с. включительно", 70, 15),
    ("Автомобили легковые с мощностью двигателя свыше 250 л.с. (свыше 183,9 кВт)", 145, 25),
    ("Мотоциклы и мотороллеры с мощностью двигателя до 20 л.с. включительно", 5, 2),
    ("Автобусы с мощностью двигателя до 200 л.с. включительно", 30, 8),
    ("Автомобили грузовые с мощностью двигателя свыше 250 л.с.", 85, 15),
    ("Катера, моторные лодки с мощностью двигателя до 100 л.с. включительно", 20, 5),
    ("Снегоходы, мотосани с мощностью двигателя до 50 л.с. включительно", 25, 5),
]

BENEFITS_FL = [
    "Пенсионеры, получающие страховую пенсию по старости",
    "Инвалиды I и II групп, а также инвалиды с детства",
    "Многодетные родители, имеющие трёх и более несовершеннолетних детей",
    "Один из родителей (законных представителей) ребёнка-инвалида",
    "Ветераны Великой Отечественной войны и ветераны боевых действий",
    "Герои Советского Союза, Герои Российской Федерации, полные кавалеры ордена Славы",
    "Граждане, подвергшиеся воздействию радиации вследствие катастрофы на Чернобыльской АЭС",
    "Владельцы транспортных средств, оснащённых исключительно электрическим двигателем",
    "Опекуны и попечители несовершеннолетних детей, оставшихся без попечения родителей",
    "Участники специальной военной операции и члены их семей",
    "Физические лица, использующие транспортные средства на газомоторном топливе",
    "Супруг (супруга) погибшего военнослужащего",
]
BENEFITS_UL = [
    "Организации, осуществляющие перевозки пассажиров городским транспортом общего пользования",
    "Сельскохозяйственные товаропроизводители, доля доходов которых от реализации сельскохозяйственной продукции составляет не менее 70 процентов",
    "Общественные организации инвалидов и организации, созданные с их участием",
    "Резиденты особой экономической зоны и участники региональных инвестиционных проектов",
    "Государственные и муниципальные учреждения, финансируемые из бюджета субъекта Российской Федерации",
    "Религиозные организации",
    "Организации, использующие транспортные средства для перевозки школьников",
    "Организации автомобильного транспорта общего пользования по маршрутам регулярных перевозок",
    "Организации, эксплуатирующие транспортные средства на газомоторном топливе",
    "Организации, осуществляющие деятельность в сфере информационных технологий",
]
BENEFITS_IP = [
    "Индивидуальные предприниматели, осуществляющие регулярные перевозки пассажиров",
    "Индивидуальные предприниматели, применяющие упрощённую систему налогообложения",
    "Главы крестьянских (фермерских) хозяйств",
    "Индивидуальные предприниматели, зарегистрированные и осуществляющие деятельность в моногородах",
    "Индивидуальные предприниматели, использующие электромобили в предпринимательской деятельности",
]
BENEFIT_KINDS = [
    "Освобождение от уплаты налога",
    "Пониженная налоговая ставка",
    "Уменьшение суммы налога на 50 процентов",
    "Налоговый вычет",
]
BENEFIT_CONDITIONS = [
    "в отношении одного транспортного средства по выбору налогоплательщика",
    "в отношении легковых автомобилей с мощностью двигателя до 150 л.с. включительно",
    "при условии использования транспортного средства в основной деятельности",
    "при отсутствии недоимки по налогам и сборам на дату подачи заявления",
    "в отношении транспортных средств, зарегистрированных на территории субъекта",
    "при условии сохранения численности работников не ниже уровня предыдущего года",
]
NPA_AUTHORITIES = [
    "Законодательное собрание субъекта Российской Федерации",
    "Государственный Совет республики",
    "Областная Дума",
    "Краевое Законодательное Собрание",
]


def generate(
    conn: sqlite3.Connection,
    year_from: int = 2015,
    year_to: int = 2024,
    seed: int = 20240101,
    tax_code: str = "tn",
) -> dict:
    """Наполняет БД правдоподобными данными по транспортному налогу."""
    rng = random.Random(seed)
    regions = [
        dict(row) for row in conn.execute(
            "SELECT code, name FROM region WHERE kind='subject' AND valid_to IS NULL"
            " ORDER BY code")
    ]
    years = list(range(year_from, year_to + 1))
    scale = {r["code"]: rng.lognormvariate(0, 0.9) + 0.15 for r in regions}

    with conn:
        conn.execute("DELETE FROM fact_form WHERE tax_code=?", (tax_code,))
        conn.execute("DELETE FROM rate WHERE tax_code=?", (tax_code,))
        conn.execute("DELETE FROM benefit WHERE tax_code=?", (tax_code,))
        conn.execute("DELETE FROM benefit_term WHERE tax_code=?", (tax_code,))

    facts = _generate_facts(conn, rng, regions, years, scale, tax_code)
    rates_n = _generate_rates(conn, rng, regions, years, tax_code)
    benefits_n = _generate_benefits(conn, rng, regions, years, tax_code)
    return {
        "regions": len(regions), "years": [year_from, year_to],
        "fact_rows": facts, "rate_rows": rates_n, "benefit_rows": benefits_n,
        "note": "данные синтетические, предназначены для проверки портала",
    }


def _generate_facts(conn, rng, regions, years, scale, tax_code) -> int:
    from .sources.forms import recompute_totals

    rows: list[tuple] = []
    for year in years:
        indicators = [
            dict(r) for r in conn.execute(
                """SELECT DISTINCT i.id, i.section, i.level, i.unit
                     FROM indicator i JOIN indicator_code c
                       ON c.indicator_id = i.id AND c.year = ? AND c.tax_code = ?
                    WHERE i.tax_code = ? ORDER BY i.section, i.sort_order""",
                (year, tax_code, tax_code))
        ]
        if not indicators:
            continue
        growth = 1.0 + 0.05 * (year - years[0])
        by_section: dict[str, list[dict]] = {}
        for indicator in indicators:
            by_section.setdefault(indicator["section"], []).append(indicator)
        for region in regions:
            factor = scale[region["code"]] * growth
            for payer in ("ul", "fl"):
                share = 0.12 if payer == "ul" else 0.88
                for section, items in by_section.items():
                    total = _section_base(section) * factor * share * rng.uniform(0.85, 1.15)
                    weights = [rng.uniform(0.4, 1.6) for _ in items]
                    total_weight = sum(w for item, w in zip(items, weights) if item["level"] > 0) or 1
                    for item, weight in zip(items, weights):
                        if item["level"] == 0:
                            value = total
                        else:
                            value = total * 0.9 * weight / total_weight
                        rows.append((year, region["code"], tax_code, item["id"], payer,
                                     round(value, 1), "demo"))
    with conn:
        conn.executemany(
            """INSERT INTO fact_form (year, region_code, tax_code, indicator_id, payer,
                                      value, source_file) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (year, region_code, tax_code, indicator_id, payer)
               DO UPDATE SET value=excluded.value""", rows)
    recompute_totals(conn, tax_code)
    return len(rows)


def _section_base(section: str) -> float:
    return {
        "1": 700_000,       # налогоплательщики, единиц
        "2": 1_100_000,     # ТС в базе, единиц
        "3": 950_000,       # ТС, по которым начислен налог, единиц
        "4": 2_400_000,     # сумма налога, тыс. руб.
        "5": 180_000,       # выпадающие доходы по льготам, тыс. руб.
    }.get(section, 100_000)


def _generate_rates(conn, rng, regions, years, tax_code) -> int:
    rows = []
    for region in regions:
        level = rng.uniform(0.7, 1.6)
        for year in years:
            drift = 1 + 0.03 * (year - years[0])
            for object_name, base, step in RATE_OBJECTS:
                if rng.random() < 0.1:
                    continue
                value = round(base * level * drift + rng.uniform(-step, step), 1)
                value = max(0.5, value)
                payer = "all" if rng.random() < 0.75 else rng.choice(["fl", "ul"])
                rows.append((
                    year, year, year, region["code"], tax_code, None, None,
                    payer, *flags_for(payer),
                    {"all": "", "fl": "Физические лица", "ul": "Юридические лица"}[payer],
                    object_name, value, f"{value} руб. с каждой лошадиной силы",
                    "руб./л.с.", "", f"О транспортном налоге в {region['name']}",
                    f"{rng.randint(10, 250)}-ОЗ", f"{year - 1}-11-{rng.randint(10, 28):02d}",
                    rng.choice(NPA_AUTHORITIES), f"{year}-01-01", f"{year}-12-31", "demo",
                ))
    with conn:
        conn.executemany(
            """INSERT INTO rate (year, year_from, year_to, region_code, tax_code, oktmo,
                                 mo_name, payer, for_fl, for_ul, for_ip, payer_text,
                                 object_name, rate_value,
                                 rate_text, rate_unit, condition, npa_name, npa_number,
                                 npa_date, npa_authority, period_from, period_to, source_file)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def _generate_benefits(conn, rng, regions, years, tax_code) -> int:
    rows = []
    pools: Sequence[tuple[str, Sequence[str]]] = (
        ("fl", BENEFITS_FL), ("ul", BENEFITS_UL), ("ip", BENEFITS_IP),
    )
    for region in regions:
        for expected_payer, pool in pools:
            count = rng.randint(2, len(pool))
            for category in rng.sample(list(pool), count):
                start = rng.choice(years[: max(1, len(years) - 2)])
                end = rng.choice([None, None, None, years[-1], start + rng.randint(1, 4)])
                if end is not None and end < start:
                    end = start
                kind = rng.choice(BENEFIT_KINDS)
                size = 100 if kind.startswith("Освобождение") else rng.choice([20, 30, 50, 70])
                payer = detect_payer(category) or expected_payer
                rows.append((
                    start, start, end, region["code"], tax_code, None, None,
                    payer, *flags_for(payer), category, kind,
                    f"{size} процентов", float(size), "%",
                    rng.choice(BENEFIT_CONDITIONS),
                    f"пункт {rng.randint(1, 9)} статьи {rng.randint(2, 12)}",
                    f"О транспортном налоге в {region['name']}",
                    f"{rng.randint(10, 250)}-ОЗ", f"{start - 1}-11-{rng.randint(10, 28):02d}",
                    rng.choice(NPA_AUTHORITIES),
                    f"{start}-01-01", f"{end}-12-31" if end else None, "demo",
                ))
    with conn:
        conn.executemany(
            """INSERT INTO benefit (year, year_from, year_to, region_code, tax_code, oktmo,
                                    mo_name, payer, for_fl, for_ul, for_ip, category, kind,
                                    size_text, size_value,
                                    size_unit, condition, basis, npa_name, npa_number,
                                    npa_date, npa_authority, period_from, period_to, source_file)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)
