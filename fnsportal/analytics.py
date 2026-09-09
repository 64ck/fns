"""Аналитический слой: профиль территории, динамика, сравнение со средним, облака слов.

Все запросы работают в трёх измерениях — год, регион, налог, — по которым
связаны оба набора данных ФНС (нормативные ставки/льготы и статотчётность).
"""
from __future__ import annotations

import math
import sqlite3
from statistics import median
from typing import Iterable, Sequence

from . import config
from .textutil import normal_form, stopword_set, terms as extract_terms

# Норма действует в году :year, если год указан явно либо год попадает в
# период действия НПА (открытый период трактуется как «по настоящее время»).
YEAR_FILTER = (
    "(COALESCE(year_from, year) IS NULL"
    " OR (COALESCE(year_from, year) <= :year"
    "     AND COALESCE(year_to, 9999) >= :year))"
)


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cur.fetchall()]


# ------------------------------------------------------------------ справочники

def taxes(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn.execute(
        "SELECT code, name, short_name, level, form_code FROM tax ORDER BY sort_order, name"
    ))


def regions(conn: sqlite3.Connection, tax_code: str | None = None, year: int | None = None) -> list[dict]:
    """Список территорий; поле has_data показывает наличие данных за год."""
    params: dict = {"tax": tax_code, "year": year}
    return _rows(conn.execute(
        """
        SELECT r.code, r.name, r.short_name, r.federal_district, r.kind,
               EXISTS (SELECT 1 FROM fact_form f
                        WHERE f.region_code = r.code
                          AND (:tax IS NULL OR f.tax_code = :tax)
                          AND (:year IS NULL OR f.year = :year))  AS has_stats,
               EXISTS (SELECT 1 FROM rate t
                        WHERE t.region_code = r.code
                          AND (:tax IS NULL OR t.tax_code = :tax)) AS has_rates,
               EXISTS (SELECT 1 FROM benefit b
                        WHERE b.region_code = r.code
                          AND (:tax IS NULL OR b.tax_code = :tax)) AS has_benefits
          FROM region r
         ORDER BY (r.kind = 'aggregate') DESC, r.name
        """, params))


def years(conn: sqlite3.Connection, tax_code: str | None = None) -> list[int]:
    rows = conn.execute(
        """SELECT year FROM (
               SELECT DISTINCT year FROM fact_form WHERE (:tax IS NULL OR tax_code = :tax)
               UNION SELECT DISTINCT year FROM rate  WHERE (:tax IS NULL OR tax_code = :tax)
               UNION SELECT DISTINCT year_from FROM rate WHERE (:tax IS NULL OR tax_code = :tax)
               UNION SELECT DISTINCT year FROM benefit WHERE (:tax IS NULL OR tax_code = :tax)
               UNION SELECT DISTINCT year_from FROM benefit WHERE (:tax IS NULL OR tax_code = :tax)
           ) WHERE year IS NOT NULL ORDER BY year""",
        {"tax": tax_code},
    ).fetchall()
    return [int(r["year"]) for r in rows]


def indicators(conn: sqlite3.Connection, tax_code: str, year: int | None = None) -> list[dict]:
    if year is None:
        return _rows(conn.execute(
            "SELECT * FROM indicator WHERE tax_code=? ORDER BY section, sort_order",
            (tax_code,)))
    return _rows(conn.execute(
        """SELECT DISTINCT i.* FROM indicator i
             JOIN indicator_code c ON c.indicator_id = i.id AND c.year = :year
            WHERE i.tax_code = :tax
            ORDER BY i.section, i.sort_order""",
        {"tax": tax_code, "year": year}))


# ------------------------------------------------------- статотчётность (факты)

def _subject_stats(conn: sqlite3.Connection, tax_code: str, year: int, payer: str) -> dict[int, dict]:
    """Средние/медианные значения показателей по всем субъектам за год."""
    rows = conn.execute(
        """SELECT f.indicator_id, f.value
             FROM fact_form f JOIN region r ON r.code = f.region_code
            WHERE f.tax_code=:tax AND f.year=:year AND f.payer=:payer
              AND r.kind='subject' AND f.value IS NOT NULL""",
        {"tax": tax_code, "year": year, "payer": payer},
    ).fetchall()
    buckets: dict[int, list[float]] = {}
    for row in rows:
        buckets.setdefault(row["indicator_id"], []).append(float(row["value"]))
    out: dict[int, dict] = {}
    for indicator_id, values in buckets.items():
        values.sort()
        out[indicator_id] = {
            "avg": sum(values) / len(values),
            "median": median(values),
            "min": values[0],
            "max": values[-1],
            "count": len(values),
            "values": values,
        }
    return out


def form_table(
    conn: sqlite3.Connection,
    tax_code: str,
    year: int,
    region_code: str,
    payers: Sequence[str] = ("ul", "fl", "total"),
) -> list[dict]:
    """Показатели формы за год по территории + сравнение со средним по субъектам."""
    values = {
        (row["indicator_id"], row["payer"]): row["value"]
        for row in conn.execute(
            """SELECT indicator_id, payer, value FROM fact_form
                WHERE tax_code=:tax AND year=:year AND region_code=:region""",
            {"tax": tax_code, "year": year, "region": region_code},
        )
    }
    stats = {payer: _subject_stats(conn, tax_code, year, payer) for payer in payers}
    out = []
    for indicator in indicators(conn, tax_code, year):
        row: dict = {
            "indicator_id": indicator["id"],
            "slug": indicator["slug"],
            "name": indicator["name"],
            "section": indicator["section"],
            "unit": indicator["unit"],
            "level": indicator["level"],
        }
        has_value = False
        for payer in payers:
            value = values.get((indicator["id"], payer))
            summary = stats[payer].get(indicator["id"])
            cell = {"value": value}
            if summary:
                cell["avg"] = summary["avg"]
                cell["median"] = summary["median"]
                cell["subjects"] = summary["count"]
                if value is not None:
                    cell["ratio_to_avg"] = value / summary["avg"] if summary["avg"] else None
                    cell["rank"] = sum(1 for v in summary["values"] if v > value) + 1
            if value is not None:
                has_value = True
            row[payer] = cell
        if has_value:
            out.append(row)
    return out


def series(
    conn: sqlite3.Connection,
    tax_code: str,
    region_code: str,
    indicator_ids: Sequence[int],
    payer: str = "total",
    year_from: int | None = None,
    year_to: int | None = None,
    compare: bool = True,
) -> dict:
    """Динамика показателей территории с линией среднего по субъектам."""
    params: dict = {
        "tax": tax_code, "region": region_code, "payer": payer,
        "yfrom": year_from or 0, "yto": year_to or 9999,
    }
    placeholders = ",".join(str(int(i)) for i in indicator_ids) or "-1"
    own = conn.execute(
        f"""SELECT year, indicator_id, value FROM fact_form
             WHERE tax_code=:tax AND region_code=:region AND payer=:payer
               AND indicator_id IN ({placeholders})
               AND year BETWEEN :yfrom AND :yto
             ORDER BY year""", params).fetchall()
    result: dict = {"region": region_code, "payer": payer, "series": {}, "years": []}
    years_set = {row["year"] for row in own}

    aggregates: dict[tuple[int, int], dict] = {}
    if compare:
        rows = conn.execute(
            f"""SELECT f.year, f.indicator_id, f.value
                  FROM fact_form f JOIN region r ON r.code = f.region_code
                 WHERE f.tax_code=:tax AND f.payer=:payer AND r.kind='subject'
                   AND f.indicator_id IN ({placeholders})
                   AND f.year BETWEEN :yfrom AND :yto
                   AND f.value IS NOT NULL""", params).fetchall()
        buckets: dict[tuple[int, int], list[float]] = {}
        for row in rows:
            buckets.setdefault((row["year"], row["indicator_id"]), []).append(float(row["value"]))
            years_set.add(row["year"])
        for key, values in buckets.items():
            values.sort()
            aggregates[key] = {
                "avg": sum(values) / len(values),
                "median": median(values),
                "min": values[0],
                "max": values[-1],
                "count": len(values),
                "values": values,
            }

    all_years = sorted(years_set)
    result["years"] = all_years
    names = {
        row["id"]: row for row in conn.execute(
            f"SELECT id, name, unit, section FROM indicator WHERE id IN ({placeholders})")
    }
    own_map = {(row["year"], row["indicator_id"]): row["value"] for row in own}
    for indicator_id in indicator_ids:
        meta = names.get(indicator_id)
        if meta is None:
            continue
        points = []
        for year in all_years:
            value = own_map.get((year, indicator_id))
            summary = aggregates.get((year, indicator_id))
            point = {"year": year, "value": value}
            if summary:
                point["avg"] = summary["avg"]
                point["median"] = summary["median"]
                point["min"] = summary["min"]
                point["max"] = summary["max"]
                point["subjects"] = summary["count"]
                if value is not None and summary["avg"]:
                    point["ratio_to_avg"] = value / summary["avg"]
                    point["rank"] = sum(1 for v in summary["values"] if v > value) + 1
            points.append(point)
        result["series"][str(indicator_id)] = {
            "indicator_id": indicator_id,
            "name": meta["name"],
            "unit": meta["unit"],
            "section": meta["section"],
            "points": points,
        }
    return result


def ranking(
    conn: sqlite3.Connection,
    tax_code: str,
    year: int,
    indicator_id: int,
    payer: str = "total",
    limit: int = 100,
) -> list[dict]:
    """Рейтинг субъектов по показателю за год."""
    return _rows(conn.execute(
        """SELECT f.region_code, r.name AS region_name, r.short_name, r.federal_district,
                  f.value
             FROM fact_form f JOIN region r ON r.code = f.region_code
            WHERE f.tax_code=:tax AND f.year=:year AND f.payer=:payer
              AND f.indicator_id=:indicator AND r.kind='subject' AND f.value IS NOT NULL
            ORDER BY f.value DESC LIMIT :limit""",
        {"tax": tax_code, "year": year, "payer": payer,
         "indicator": indicator_id, "limit": limit}))


# --------------------------------------------------------------- ставки и льготы

def rates(
    conn: sqlite3.Connection,
    tax_code: str,
    year: int,
    region_code: str,
    payer: str | None = None,
    search: str = "",
    limit: int = 500,
    offset: int = 0,
) -> dict:
    params = {
        "tax": tax_code, "year": year, "region": region_code,
        "payer": payer, "q": f"%{search.lower()}%", "limit": limit, "offset": offset,
    }
    where = f"""WHERE tax_code=:tax AND region_code=:region AND {YEAR_FILTER}
                  AND (:payer IS NULL OR payer=:payer)
                  AND (:q = '%%' OR lower(COALESCE(object_name,'') || ' ' ||
                       COALESCE(payer_text,'') || ' ' || COALESCE(npa_name,'')) LIKE :q)"""
    total = conn.execute(f"SELECT COUNT(*) AS n FROM rate {where}", params).fetchone()["n"]
    items = _rows(conn.execute(
        f"""SELECT id, year, year_from, year_to, oktmo, mo_name, payer, payer_text,
                   object_name, rate_value, rate_text, rate_unit, condition,
                   npa_name, npa_number, npa_date, npa_authority
              FROM rate {where}
             ORDER BY (mo_name IS NOT NULL AND mo_name <> ''), object_name, rate_value
             LIMIT :limit OFFSET :offset""", params))
    return {"total": total, "items": items}


def benefits(
    conn: sqlite3.Connection,
    tax_code: str,
    year: int,
    region_code: str,
    payer: str | None = None,
    search: str = "",
    limit: int = 500,
    offset: int = 0,
) -> dict:
    params = {
        "tax": tax_code, "year": year, "region": region_code,
        "payer": payer, "q": f"%{search.lower()}%", "limit": limit, "offset": offset,
    }
    where = f"""WHERE tax_code=:tax AND region_code=:region AND {YEAR_FILTER}
                  AND (:payer IS NULL OR payer=:payer)
                  AND (:q = '%%' OR lower(COALESCE(category,'') || ' ' ||
                       COALESCE(kind,'') || ' ' || COALESCE(condition,'') || ' ' ||
                       COALESCE(basis,'')) LIKE :q)"""
    total = conn.execute(f"SELECT COUNT(*) AS n FROM benefit {where}", params).fetchone()["n"]
    items = _rows(conn.execute(
        f"""SELECT id, year, year_from, year_to, oktmo, mo_name, payer, category, kind,
                   size_text, size_value, size_unit, condition, basis,
                   npa_name, npa_number, npa_date, npa_authority
              FROM benefit {where}
             ORDER BY payer, category LIMIT :limit OFFSET :offset""", params))
    by_payer = {
        row["payer"]: row["n"] for row in conn.execute(
            f"""SELECT payer, COUNT(*) AS n FROM benefit
                WHERE tax_code=:tax AND region_code=:region AND {YEAR_FILTER}
                GROUP BY payer""",
            {"tax": tax_code, "year": year, "region": region_code})
    }
    return {"total": total, "items": items, "by_payer": by_payer}


def profile(conn: sqlite3.Connection, tax_code: str, year: int, region_code: str) -> dict:
    """Карточка территории: ключевые цифры формы + объём нормативных данных."""
    region = conn.execute("SELECT * FROM region WHERE code=?", (region_code,)).fetchone()
    table = form_table(conn, tax_code, year, region_code)
    key_rows = [row for row in table if row["level"] == 0]
    counts = conn.execute(
        f"""SELECT
              (SELECT COUNT(*) FROM rate
                WHERE tax_code=:tax AND region_code=:region AND {YEAR_FILTER}) AS rates,
              (SELECT COUNT(*) FROM benefit
                WHERE tax_code=:tax AND region_code=:region AND {YEAR_FILTER}) AS benefits,
              (SELECT COUNT(DISTINCT COALESCE(oktmo,'')) FROM rate
                WHERE tax_code=:tax AND region_code=:region AND {YEAR_FILTER}) AS municipalities
        """, {"tax": tax_code, "year": year, "region": region_code}).fetchone()
    benefits_by_payer = {
        row["payer"]: row["n"] for row in conn.execute(
            f"""SELECT payer, COUNT(*) AS n FROM benefit
                 WHERE tax_code=:tax AND region_code=:region AND {YEAR_FILTER}
                 GROUP BY payer""",
            {"tax": tax_code, "year": year, "region": region_code})
    }
    return {
        "region": dict(region) if region else {"code": region_code, "name": region_code},
        "tax_code": tax_code,
        "year": year,
        "key_indicators": key_rows,
        "counts": dict(counts) if counts else {},
        "benefits_by_payer": benefits_by_payer,
        "available_years": years(conn, tax_code),
    }


# --------------------------------------------------------------- облака слов

TERM_FIELDS = ("category", "kind", "condition")


def build_terms(
    conn: sqlite3.Connection,
    tax_code: str | None = None,
    *,
    top_per_bucket: int = 400,
    fields: Sequence[str] = ("category",),
    bigrams: bool = True,
    extra_stopwords: Sequence[str] = (),
    min_year: int = 2000,
    max_year: int | None = None,
    flush_every: int = 500_000,
    progress=None,
) -> dict:
    """Строит частотный словарь по текстам льгот (таблица benefit_term).

    По умолчанию берётся только текст категории льготника: именно он отвечает
    на вопрос «кому предоставлена льгота». Условия предоставления можно
    подключить параметром fields — тогда в облако попадут и формулировки
    условий (`--fields category,condition` в CLI).

    Каждая льгота учитывается во всех годах своего периода действия, поэтому
    облако слов за конкретный год показывает реально действовавшие льготы.
    Для ускорения в каждом «ведре» (налог × год × регион × категория) остаются
    только `top_per_bucket` самых частых терминов — этого достаточно для облака.
    """
    from datetime import date

    max_year = max_year or date.today().year + 1
    stops = stopword_set(tuple(extra_stopwords))
    where = "WHERE tax_code = ?" if tax_code else ""
    params = (tax_code,) if tax_code else ()

    with conn:
        conn.execute(f"DELETE FROM benefit_term {where}", params)

    columns = ", ".join(f"COALESCE({f}, '')" for f in fields if f in TERM_FIELDS)
    cur = conn.execute(
        f"""SELECT tax_code, region_code, payer,
                   COALESCE(year_from, year) AS y_from,
                   COALESCE(year_to, COALESCE(year_from, year)) AS y_to,
                   {columns} AS text
              FROM benefit {where}""", params)

    counts: dict[tuple, list[int]] = {}
    display: dict[str, dict[str, int]] = {}
    processed = 0
    saved = 0

    def flush() -> None:
        nonlocal saved
        if not counts:
            return
        payload = []
        for (tax, year, region, payer, term, is_bigram), (tf, df) in counts.items():
            surfaces = display.get(term)
            best = max(surfaces, key=surfaces.get) if surfaces else term
            payload.append((tax, year, region, payer, term, best, is_bigram, tf, df))
        with conn:
            conn.executemany(
                """INSERT INTO benefit_term
                       (tax_code, year, region_code, payer, term, display, is_bigram,
                        term_freq, doc_freq)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (tax_code, year, region_code, payer, term) DO UPDATE SET
                       term_freq = term_freq + excluded.term_freq,
                       doc_freq  = doc_freq  + excluded.doc_freq""",
                payload)
        saved += len(payload)
        counts.clear()

    for row in cur:
        processed += 1
        text = row["text"]
        if not text or not text.strip():
            continue
        y_from = row["y_from"] or 0
        y_to = row["y_to"] or y_from
        if y_from:
            y_from = max(min_year, int(y_from))
            y_to = min(max_year, int(y_to) if y_to else y_from)
            year_range = range(y_from, max(y_from, y_to) + 1)
        else:
            year_range = range(0, 1)          # год неизвестен
        local: dict[tuple[str, bool], int] = {}
        for norm, surface, is_bigram in extract_terms(text, stops, bigrams=bigrams):
            local[(norm, is_bigram)] = local.get((norm, is_bigram), 0) + 1
            bucket = display.setdefault(norm, {})
            bucket[surface] = bucket.get(surface, 0) + 1
        for year in year_range:
            for (norm, is_bigram), freq in local.items():
                key = (row["tax_code"], year, row["region_code"], row["payer"], norm, int(is_bigram))
                cell = counts.get(key)
                if cell is None:
                    counts[key] = [freq, 1]
                else:
                    cell[0] += freq
                    cell[1] += 1
        if len(counts) >= flush_every:
            flush()
            if progress:
                progress(processed)

    flush()

    # агрегат «Российская Федерация» — точная сумма по всем регионам
    with conn:
        conn.execute(
            f"""INSERT INTO benefit_term
                    (tax_code, year, region_code, payer, term, display, is_bigram,
                     term_freq, doc_freq)
                SELECT tax_code, year, :rf, payer, term,
                       (SELECT display FROM benefit_term t2
                         WHERE t2.term = t.term AND t2.tax_code = t.tax_code
                         ORDER BY t2.term_freq DESC LIMIT 1),
                       is_bigram, SUM(term_freq), SUM(doc_freq)
                  FROM benefit_term t
                 WHERE region_code <> :rf {'AND tax_code = :tax' if tax_code else ''}
                 GROUP BY tax_code, year, payer, term, is_bigram
                ON CONFLICT (tax_code, year, region_code, payer, term) DO UPDATE SET
                    term_freq = excluded.term_freq, doc_freq = excluded.doc_freq""",
            {"rf": config.RF_CODE, "tax": tax_code})

        # оставляем только самые частые термины в каждом ведре
        conn.execute(
            """DELETE FROM benefit_term WHERE rowid IN (
                   SELECT rowid FROM (
                       SELECT rowid, ROW_NUMBER() OVER (
                           PARTITION BY tax_code, year, region_code, payer
                           ORDER BY term_freq DESC, doc_freq DESC) AS rn
                         FROM benefit_term)
                    WHERE rn > ?)""", (top_per_bucket,))
        conn.execute("ANALYZE")
    remaining = conn.execute("SELECT COUNT(*) AS n FROM benefit_term").fetchone()["n"]
    return {"benefits_processed": processed, "terms_written": saved, "terms_kept": remaining}


def wordcloud(
    conn: sqlite3.Connection,
    tax_code: str,
    payer: str,
    years_selected: Sequence[int] = (),
    regions_selected: Sequence[str] = (),
    limit: int = 120,
    mode: str = "freq",
    include_bigrams: bool = True,
) -> dict:
    """Облако слов по льготам: частоты либо «отличительность» категории.

    mode='freq'        — самые частые слова выбранной категории плательщиков;
    mode='distinctive' — слова, которыми эта категория отличается от остальных
                         (log-odds относительно других категорий).
    """
    where = ["tax_code = :tax"]
    params: dict = {"tax": tax_code, "limit": limit}
    if years_selected:
        where.append("year IN (%s)" % ",".join(str(int(y)) for y in years_selected))
    if regions_selected:
        placeholders = ",".join(f":r{i}" for i in range(len(regions_selected)))
        where.append(f"region_code IN ({placeholders})")
        params.update({f"r{i}": code for i, code in enumerate(regions_selected)})
    else:
        where.append("region_code = :rf")
        params["rf"] = config.RF_CODE
    if not include_bigrams:
        where.append("is_bigram = 0")
    base = " AND ".join(where)

    rows = conn.execute(
        f"""SELECT term, payer,
                   MAX(display) AS display, MAX(is_bigram) AS is_bigram,
                   SUM(term_freq) AS tf, SUM(doc_freq) AS df
              FROM benefit_term WHERE {base}
             GROUP BY term, payer""", params).fetchall()

    target: dict[str, dict] = {}
    other: dict[str, float] = {}
    total_target = 0.0
    total_other = 0.0
    for row in rows:
        if row["payer"] == payer:
            target[row["term"]] = {
                "term": row["term"], "text": row["display"] or row["term"],
                "freq": row["tf"], "docs": row["df"], "bigram": bool(row["is_bigram"]),
            }
            total_target += row["tf"]
        else:
            other[row["term"]] = other.get(row["term"], 0.0) + row["tf"]
            total_other += row["tf"]

    items = list(target.values())
    if mode == "distinctive" and total_target and total_other:
        for item in items:
            p_target = item["freq"] / total_target
            p_other = (other.get(item["term"], 0.0) + 1) / (total_other + 1)
            item["score"] = item["freq"] * max(0.0, math.log(p_target / p_other))
        items.sort(key=lambda i: (-i["score"], -i["freq"]))
    else:
        for item in items:
            item["score"] = float(item["freq"])
        items.sort(key=lambda i: -i["freq"])

    return {
        "tax_code": tax_code,
        "payer": payer,
        "mode": mode,
        "years": list(years_selected),
        "regions": list(regions_selected) or [config.RF_CODE],
        "total_terms": len(items),
        "items": items[:limit],
    }
