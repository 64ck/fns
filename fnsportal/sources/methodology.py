"""Разбор методички по сбору данных формы 5-ТН.

Формы статотчётности ФНС меняются от года к году: один и тот же показатель
переезжает на другой код строки, а до 2007 г. юридические и физические лица
жили в одной таблице в разных графах. Методичка (xlsx, по листу на каждый год)
задаёт соответствие «год -> код строки -> показатель», а этот модуль
превращает её в канонический справочник показателей.

Ключевая идея: сквозной идентификатор показателя (slug) строится из номера
раздела формы и названия показателя, а группа («Автомобили легковые...»)
добавляется только к тем названиям, которые без неё неоднозначны
(«до 100 л.с. включительно» встречается у 5 видов транспорта). Благодаря
этому один и тот же показатель 2006 и 2025 года получает одинаковый slug и
попадает в общий динамический ряд.
"""
from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from ..textutil import clean, norm_key, parse_year

SECTION_RE = re.compile(r"^(\d+)\s*\.\s*(.+)$", re.S)
SHEET_RE = re.compile(r"^(\d{4})\s*[-_ ]\s*(.+)$")

# Раздел формы 5-ТН -> единица измерения по умолчанию
SECTION_UNITS = {
    "1": "единиц",
    "2": "единиц",
    "3": "единиц",
    "4": "тыс. руб.",
    "5": "тыс. руб.",
}

HEADER_ALIASES = {
    "name": ("показатель",),
    "code_ul": ("код строки для юридических лиц", "код строки юл", "код юл"),
    "code_fl": ("код строки для физических лиц", "код строки фл", "код фл"),
    "year": ("год формы", "год"),
    "col_ul": ("столбец для юридических лиц", "графа для юридических лиц"),
    "col_fl": ("столбец для физических лиц", "графа для физических лиц"),
}


@dataclass
class MapRow:
    year: int
    tax_code: str
    section: str
    level: int
    group: str
    name: str
    slug: str = ""
    unit: str = ""
    code_ul: str = ""
    code_fl: str = ""
    col_ul: int | None = None
    col_fl: int | None = None
    sort_order: int = 0


@dataclass
class Methodology:
    rows: list[MapRow] = field(default_factory=list)

    @property
    def years(self) -> list[int]:
        return sorted({r.year for r in self.rows})


def _match_header(cells: list[str]) -> dict[str, int] | None:
    """Ищет строку заголовка и возвращает {поле: индекс колонки}."""
    normalized = [norm_key(c) for c in cells]
    found: dict[str, int] = {}
    for field_name, aliases in HEADER_ALIASES.items():
        for idx, value in enumerate(normalized):
            if not value:
                continue
            if any(value.startswith(norm_key(a)) for a in aliases):
                found.setdefault(field_name, idx)
                break
    if "name" in found and ("code_ul" in found or "code_fl" in found):
        return found
    return None


def _code(value: object) -> str:
    text = clean(value)
    if not text:
        return ""
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    return text.split(".")[0].strip()


def parse_workbook(path: Path, default_tax: str = "tn") -> Methodology:
    """Читает xlsx-методичку (по листу на год) в набор строк соответствия."""
    import openpyxl  # ленивый импорт: нужен только на этапе ETL

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    meth = Methodology()
    for sheet in wb.worksheets:
        tax_code = default_tax
        sheet_year = None
        m = SHEET_RE.match(clean(sheet.title))
        if m:
            sheet_year = int(m.group(1))
        header: dict[str, int] | None = None
        section = ""
        group = ""
        seen_sections: set[str] = set()
        order = 0
        for raw in sheet.iter_rows(values_only=True):
            cells = ["" if c is None else c for c in raw]
            if header is None:
                header = _match_header([clean(c) for c in cells])
                continue

            def cell(key: str) -> object:
                idx = header.get(key) if header else None
                return cells[idx] if idx is not None and idx < len(cells) else ""

            name = clean(cell("name"))
            if not name:
                continue
            code_ul, code_fl = _code(cell("code_ul")), _code(cell("code_fl"))
            if not code_ul and not code_fl:
                continue
            year = parse_year(cell("year")) or sheet_year
            if not year:
                continue

            has_prefix = False
            m_sec = SECTION_RE.match(name)
            if m_sec:
                has_prefix = True
                section = m_sec.group(1)
                name = clean(m_sec.group(2))

            first_in_section = has_prefix and section not in seen_sections
            if has_prefix:
                seen_sections.add(section)
            # Уровень вложенности: строки с номером раздела в названии — это либо
            # итог раздела, либо заголовок группы («Автомобили легковые ...:»).
            # Остальные строки различаем по коду: «круглый» код (1310, 1350, 040)
            # — это самостоятельная строка раздела, она закрывает открытую группу,
            # а некруглый (1312, 1338) — элемент внутри группы.
            round_code = (code_ul or code_fl).endswith("0")
            if first_in_section:
                level, group = 0, ""
            elif has_prefix:
                level, group = 2, name
            elif round_code or not group:
                level, group = 1, ""
            else:
                level = 3

            order += 1
            meth.rows.append(
                MapRow(
                    year=year,
                    tax_code=tax_code,
                    section=section,
                    level=level,
                    group=group if level == 3 else "",
                    name=name,
                    code_ul=code_ul,
                    code_fl=code_fl,
                    col_ul=int(cell("col_ul")) if clean(cell("col_ul")) else None,
                    col_fl=int(cell("col_fl")) if clean(cell("col_fl")) else None,
                    sort_order=order,
                )
            )
    wb.close()
    _assign_slugs(meth)
    return meth


def _assign_slugs(meth: Methodology) -> None:
    """Присваивает сквозные slug'и, добавляя группу только при неоднозначности."""
    by_section_name: dict[tuple[str, str], set[str]] = {}
    for row in meth.rows:
        key = (row.section, norm_key(row.name))
        by_section_name.setdefault(key, set()).add(norm_key(row.group))
    ambiguous = {key for key, groups in by_section_name.items() if len(groups) > 1}

    for row in meth.rows:
        base = norm_key(row.name).replace(" ", "-")
        key = (row.section, norm_key(row.name))
        if key in ambiguous and row.group:
            base = f"{norm_key(row.group).replace(' ', '-')}--{base}"
        row.slug = f"s{row.section or '0'}--{base}"[:180]
        row.unit = _detect_unit(row)

    # финальная страховка от коллизий внутри одного года
    for year in meth.years:
        seen: dict[str, MapRow] = {}
        for row in [r for r in meth.rows if r.year == year]:
            if row.slug in seen and seen[row.slug].name != row.name:
                suffix = 2
                while f"{row.slug}--{suffix}" in seen:
                    suffix += 1
                row.slug = f"{row.slug}--{suffix}"
            seen[row.slug] = row


def _detect_unit(row: MapRow) -> str:
    lowered = row.name.lower()
    if "тыс. руб" in lowered or "тыс.руб" in lowered:
        return "тыс. руб."
    if "единиц" in lowered:
        return "единиц"
    return SECTION_UNITS.get(row.section, "")


def write_map_csv(meth: Methodology, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "tax_code", "year", "section", "level", "group", "name", "slug",
            "unit", "code_ul", "code_fl", "col_ul", "col_fl", "sort_order",
        ])
        for row in meth.rows:
            writer.writerow([
                row.tax_code, row.year, row.section, row.level, row.group,
                row.name, row.slug, row.unit, row.code_ul, row.code_fl,
                row.col_ul if row.col_ul is not None else "",
                row.col_fl if row.col_fl is not None else "",
                row.sort_order,
            ])
    return path


def read_map_csv(path: Path) -> Methodology:
    meth = Methodology()
    with path.open(encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            meth.rows.append(
                MapRow(
                    year=int(rec["year"]),
                    tax_code=rec["tax_code"],
                    section=rec["section"],
                    level=int(rec["level"] or 0),
                    group=rec["group"],
                    name=rec["name"],
                    slug=rec["slug"],
                    unit=rec["unit"],
                    code_ul=rec["code_ul"],
                    code_fl=rec["code_fl"],
                    col_ul=int(rec["col_ul"]) if rec["col_ul"] else None,
                    col_fl=int(rec["col_fl"]) if rec["col_fl"] else None,
                    sort_order=int(rec["sort_order"] or 0),
                )
            )
    return meth


def load_into_db(conn: sqlite3.Connection, meth: Methodology) -> tuple[int, int]:
    """Записывает справочник показателей и карту кодов строк по годам."""
    latest = max(meth.years) if meth.years else 0
    indicators: dict[tuple[str, str], MapRow] = {}
    for row in sorted(meth.rows, key=lambda r: (r.year, r.sort_order)):
        key = (row.tax_code, row.slug)
        # имя показателя берём из самой свежей формы, где он встречается
        if key not in indicators or row.year >= indicators[key].year:
            indicators[key] = row

    with conn:
        for (tax_code, slug), row in indicators.items():
            full_name = f"{row.group}: {row.name}" if row.group else row.name
            conn.execute(
                """INSERT INTO indicator (tax_code, slug, name, section, unit, parent_slug,
                                          level, sort_order)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT (tax_code, slug) DO UPDATE SET
                       name=excluded.name, section=excluded.section, unit=excluded.unit,
                       level=excluded.level, sort_order=excluded.sort_order""",
                (tax_code, slug, full_name, row.section, row.unit, None, row.level,
                 row.sort_order if row.year == latest else 10_000 + row.sort_order),
            )
        ids = {
            (r["tax_code"], r["slug"]): r["id"]
            for r in conn.execute("SELECT id, tax_code, slug FROM indicator")
        }
        codes = 0
        for row in meth.rows:
            indicator_id = ids[(row.tax_code, row.slug)]
            for payer, code, col in (
                ("ul", row.code_ul, row.col_ul),
                ("fl", row.code_fl, row.col_fl),
            ):
                if not code:
                    continue
                conn.execute(
                    """INSERT INTO indicator_code
                           (tax_code, year, payer, row_code, col_index, indicator_id)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT (tax_code, year, payer, row_code)
                       DO UPDATE SET indicator_id=excluded.indicator_id,
                                     col_index=excluded.col_index""",
                    (row.tax_code, row.year, payer, code, col, indicator_id),
                )
                codes += 1
    return len(indicators), codes
