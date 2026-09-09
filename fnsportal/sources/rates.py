"""ETL набора «Ставки и льготы по имущественным налогам» (opendata ФНС).

Набор https://www.nalog.gov.ru/opendata/7707329152-taxrates/ выгружается в
csv/xls/html; HTML-версия занимает около 3 ГБ, поэтому загрузка идёт потоком,
пакетами по 20 000 строк, без чтения файла в память.

Сопоставление колонок вынесено в reference/column_aliases.json: заголовки в
выгрузках ФНС меняются, и подстроить загрузчик под новый файл можно правкой
одного json без изменения кода.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .. import config, db
from ..textutil import clean, detect_payer, norm_key, parse_number, parse_year
from . import tablestream

BATCH = 20_000
MAX_YEAR = date.today().year + 1

RATE_COLUMNS = (
    "year", "year_from", "year_to", "region_code", "tax_code", "oktmo", "mo_name",
    "payer", "payer_text", "object_name", "rate_value", "rate_text", "rate_unit",
    "condition", "npa_name", "npa_number", "npa_date", "npa_authority",
    "period_from", "period_to", "source_file",
)
BENEFIT_COLUMNS = (
    "year", "year_from", "year_to", "region_code", "tax_code", "oktmo", "mo_name",
    "payer", "category", "kind", "size_text", "size_value", "size_unit",
    "condition", "basis", "npa_name", "npa_number", "npa_date", "npa_authority",
    "period_from", "period_to", "source_file",
)

_DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})|(\d{4})-(\d{2})-(\d{2})")


@dataclass
class LoadStats:
    rows_read: int = 0
    rates: int = 0
    benefits: int = 0
    skipped: int = 0
    unmapped_regions: set[str] = field(default_factory=set)
    unmapped_taxes: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "rows_read": self.rows_read,
            "rates": self.rates,
            "benefits": self.benefits,
            "skipped": self.skipped,
            "unmapped_regions": sorted(self.unmapped_regions)[:20],
            "unmapped_taxes": sorted(self.unmapped_taxes)[:20],
        }


# ------------------------------------------------------------- сопоставления

def load_aliases(path: Path | None = None) -> dict[str, list[str]]:
    path = path or config.REFERENCE_DIR / "column_aliases.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: [norm_key(a) for a in v] for k, v in data.items() if not k.startswith("_")}


def map_columns(header: Sequence[str], aliases: dict[str, list[str]]) -> dict[str, str]:
    """Строит соответствие «поле портала -> имя колонки в файле».

    Сначала считаются все пары (поле, колонка) с оценкой совпадения:
    3 — точное совпадение заголовка с алиасом, 2 — заголовок начинается с
    алиаса, 1 — алиас входит в заголовок. Затем пары назначаются от лучших к
    худшим, при равной оценке выигрывает более длинный (более специфичный)
    алиас. Так «Категория налогоплательщика» достаётся полю benefit_category,
    а не payer_text, у которого есть общий алиас «налогоплательщик».
    """
    keys = [(name, norm_key(name)) for name in header if clean(name)]
    scored: list[tuple[int, int, str, str]] = []
    for field_name, variants in aliases.items():
        for variant in variants:
            if not variant:
                continue
            for original, key in keys:
                if not key:
                    continue
                if key == variant:
                    score = 3
                elif key.startswith(variant):
                    score = 2
                elif variant in key:
                    score = 1
                else:
                    continue
                scored.append((score, len(variant), field_name, original))

    scored.sort(key=lambda item: (-item[0], -item[1]))
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for _score, _length, field_name, column in scored:
        if field_name in mapping or column in used:
            continue
        mapping[field_name] = column
        used.add(column)
    return mapping


class RegionResolver:
    """Определяет код региона по коду, названию или (как запасной вариант) ОКТМО."""

    def __init__(self, conn: sqlite3.Connection):
        self.by_name: dict[str, str] = {}
        self.known: set[str] = set()
        for row in conn.execute("SELECT code, name, short_name FROM region"):
            self.known.add(row["code"])
            for value in (row["name"], row["short_name"]):
                if value:
                    self.by_name[self._name_key(value)] = row["code"]
        self.oktmo_prefix: dict[str, str] = {}   # выучивается по самим данным
        self.oktmo_votes: dict[str, dict[str, int]] = {}

    @staticmethod
    def _name_key(name: str) -> str:
        key = norm_key(name)
        for prefix in ("республика ", "г ", "город ", "область ", "край "):
            if key.startswith(prefix):
                key = key[len(prefix):]
        for suffix in (" республика", " область", " край", " автономный округ", " автономная область"):
            if key.endswith(suffix):
                key = key[: -len(suffix)]
        return key.strip()

    def resolve(self, code: str = "", name: str = "", oktmo: str = "") -> str | None:
        code = clean(code)
        if code:
            digits = re.sub(r"\D", "", code)
            if digits:
                normalized = digits[:2].zfill(2) if len(digits) <= 2 else digits[:2]
                if normalized in self.known:
                    self._learn(oktmo, normalized)
                    return normalized
        if name:
            key = self._name_key(name)
            if key in self.by_name:
                resolved = self.by_name[key]
                self._learn(oktmo, resolved)
                return resolved
            for candidate, value in self.by_name.items():
                if candidate and (candidate in key or key in candidate) and len(candidate) > 4:
                    self._learn(oktmo, value)
                    return value
        if oktmo:
            prefix = re.sub(r"\D", "", oktmo)[:2]
            if prefix in self.oktmo_prefix:
                return self.oktmo_prefix[prefix]
        return None

    def _learn(self, oktmo: str, region_code: str) -> None:
        prefix = re.sub(r"\D", "", clean(oktmo))[:2]
        if not prefix:
            return
        votes = self.oktmo_votes.setdefault(prefix, {})
        votes[region_code] = votes.get(region_code, 0) + 1
        self.oktmo_prefix[prefix] = max(votes, key=votes.get)


class TaxResolver:
    def __init__(self, conn: sqlite3.Connection, aliases_path: Path | None = None):
        self.aliases: list[tuple[str, str]] = []
        for row in conn.execute("SELECT code, name, short_name FROM tax"):
            for value in (row["name"], row["short_name"], row["code"]):
                if value:
                    self.aliases.append((norm_key(value), row["code"]))
        path = aliases_path or config.REFERENCE_DIR / "tax_aliases.csv"
        if path.exists():
            import csv as _csv

            with path.open(encoding="utf-8") as fh:
                for rec in _csv.DictReader(fh):
                    self.aliases.append((norm_key(rec["alias"]), rec["tax_code"]))
        self.aliases.sort(key=lambda pair: -len(pair[0]))

    def resolve(self, text: str) -> str | None:
        key = norm_key(text)
        if not key:
            return None
        for alias, code in self.aliases:
            if alias and alias in key:
                return code
        return None


def parse_date(value: object) -> str | None:
    text = clean(value)
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    if m.group(1):
        day, month, year = m.group(1), m.group(2), m.group(3)
    else:
        year, month, day = m.group(4), m.group(5), m.group(6)
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def year_bounds(record: dict, cols: dict[str, str]) -> tuple[int | None, int | None, int | None]:
    """Возвращает (год, год_с, год_по) для строки НПА."""
    explicit = parse_year(record.get(cols.get("period_year", ""), ""))
    date_from = parse_date(record.get(cols.get("date_from", ""), ""))
    date_to = parse_date(record.get(cols.get("date_to", ""), ""))
    npa_date = parse_date(record.get(cols.get("npa_date", ""), ""))

    year_from = int(date_from[:4]) if date_from else None
    year_to = int(date_to[:4]) if date_to else None
    if explicit:
        return explicit, year_from or explicit, year_to or explicit
    if year_from is None and npa_date:
        year_from = int(npa_date[:4])
    return year_from, year_from, year_to


# ------------------------------------------------------------------- загрузка

def load_file(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    tax_code: str | None = None,
    default_year: int | None = None,
    limit: int | None = None,
    progress: object | None = None,
) -> LoadStats:
    """Загружает файл ставок/льгот в БД. Работает потоком, память O(BATCH)."""
    path = Path(path)
    aliases = load_aliases()
    regions = RegionResolver(conn)
    taxes = TaxResolver(conn)
    stats = LoadStats()
    rate_batch: list[tuple] = []
    benefit_batch: list[tuple] = []

    rate_sql = (
        f"INSERT INTO rate ({','.join(RATE_COLUMNS)}) "
        f"VALUES ({','.join('?' * len(RATE_COLUMNS))})"
    )
    benefit_sql = (
        f"INSERT INTO benefit ({','.join(BENEFIT_COLUMNS)}) "
        f"VALUES ({','.join('?' * len(BENEFIT_COLUMNS))})"
    )

    def flush() -> None:
        if rate_batch:
            conn.executemany(rate_sql, rate_batch)
            rate_batch.clear()
        if benefit_batch:
            conn.executemany(benefit_sql, benefit_batch)
            benefit_batch.clear()
        conn.commit()

    with db.bulk_insert(conn):
        for table in tablestream.iter_tables(path):
            rows = table.rows
            idx, header, buffered = tablestream.find_header(
                rows, required=["налог", "регион", "ставка", "льгот"]
            )
            if not header:
                continue
            cols = map_columns(header, aliases)
            if not cols.get("region_code") and not cols.get("region_name") and not cols.get("oktmo"):
                continue
            names = header

            def records() -> Iterator[dict[str, str]]:
                for row in buffered[(idx or 0) + 1:]:
                    yield {names[i]: v for i, v in enumerate(row) if i < len(names)}
                for row in rows:
                    yield {names[i]: v for i, v in enumerate(row) if i < len(names)}

            for record in records():
                stats.rows_read += 1
                if limit and stats.rows_read > limit:
                    break
                parsed = parse_record(
                    record, cols, regions, taxes,
                    tax_code=tax_code, default_year=default_year, source=path.name,
                )
                if parsed is None:
                    stats.skipped += 1
                    continue
                rate_row, benefit_row, miss = parsed
                if miss:
                    getattr(stats, miss[0]).add(miss[1])
                if rate_row:
                    rate_batch.append(rate_row)
                    stats.rates += 1
                if benefit_row:
                    benefit_batch.append(benefit_row)
                    stats.benefits += 1
                if len(rate_batch) + len(benefit_batch) >= BATCH:
                    flush()
                    if progress:
                        progress(stats)
        flush()
    db.log_load(conn, "rates", str(path), stats.rows_read, stats.rates + stats.benefits,
                json.dumps(stats.as_dict(), ensure_ascii=False))
    return stats


def parse_record(
    record: dict[str, str],
    cols: dict[str, str],
    regions: RegionResolver,
    taxes: TaxResolver,
    *,
    tax_code: str | None,
    default_year: int | None,
    source: str,
) -> tuple[tuple | None, tuple | None, tuple[str, str] | None] | None:
    """Превращает строку выгрузки в кортежи для таблиц rate и benefit."""
    def value(field_name: str) -> str:
        column = cols.get(field_name)
        return clean(record.get(column, "")) if column else ""

    region = regions.resolve(value("region_code"), value("region_name"), value("oktmo"))
    miss: tuple[str, str] | None = None
    if region is None:
        raw = value("region_name") or value("region_code")
        if raw:
            miss = ("unmapped_regions", raw[:80])
        return None if not raw else (None, None, miss)

    tax = tax_code or taxes.resolve(value("tax_name"))
    if tax is None:
        raw = value("tax_name")
        if raw:
            return None, None, ("unmapped_taxes", raw[:80])
        return None

    year, year_from, year_to = year_bounds(record, cols)
    if year is None:
        year = default_year
        year_from = year_from or default_year
    if year_to is not None and year_to > MAX_YEAR + 50:
        year_to = None

    oktmo = value("oktmo")
    mo_name = value("mo_name")
    npa = (value("npa_name"), value("npa_number"), parse_date(value("npa_date")), value("npa_authority"))
    period = (parse_date(value("date_from")), parse_date(value("date_to")))
    condition = value("condition")

    rate_text = value("rate_value")
    object_name = value("object_name")
    payer_text = value("payer_text")
    rate_row = None
    if rate_text or object_name:
        rate_row = (
            year, year_from, year_to, region, tax, oktmo, mo_name,
            detect_payer(payer_text, object_name), payer_text, object_name,
            parse_number(rate_text), rate_text, value("rate_unit"), condition,
            *npa, *period, source,
        )

    category = value("benefit_category")
    kind = value("benefit_kind")
    size_text = value("benefit_size")
    basis = value("basis")
    benefit_row = None
    if category or kind or size_text:
        benefit_row = (
            year, year_from, year_to, region, tax, oktmo, mo_name,
            detect_payer(payer_text, category, kind), category, kind, size_text,
            parse_number(size_text), value("benefit_unit"), condition, basis,
            *npa, *period, source,
        )
    if rate_row is None and benefit_row is None:
        return None
    return rate_row, benefit_row, miss
