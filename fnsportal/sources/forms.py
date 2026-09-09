"""ETL форм статистической налоговой отчётности (5-ТН и аналогичные).

Файлы формы публикуются по каждому региону и каждому году отдельно, а сама
форма со временем меняется. Загрузчик не полагается на порядок строк: он
ищет в файле коды строк (графа «Б») и переводит их в сквозные показатели по
карте из методички (`indicator_code`). Поэтому один и тот же код процедуры
работает и с формой 2006 года (юрлица и физлица в разных графах одной
таблицы), и с формой 2025 года (отдельные коды 1xxx / 2xxx).

Год и регион определяются в таком порядке:
  1) явно переданные параметры;
  2) имя файла и путь (`5tn_2019_77.xls`, `.../2019/77_Москва.xls`);
  3) титульные строки внутри файла («по Московской области», «код 50»).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .. import db
from ..textutil import clean, norm_key, parse_number, parse_year
from . import tablestream
from .rates import RegionResolver

CODE_RE = re.compile(r"^\d{3,4}$")
MAX_TITLE_ROWS = 25


@dataclass
class FormStats:
    file: str = ""
    year: int | None = None
    region_code: str | None = None
    tax_code: str = ""
    rows_read: int = 0
    values: int = 0
    unknown_codes: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "file": self.file,
            "year": self.year,
            "region_code": self.region_code,
            "tax_code": self.tax_code,
            "rows_read": self.rows_read,
            "values": self.values,
            "unknown_codes": sorted(self.unknown_codes)[:20],
            "problems": self.problems,
        }


class CodeMap:
    """Карта «код строки -> (показатель, категория, графа)» для года и налога."""

    def __init__(self, conn: sqlite3.Connection, tax_code: str, year: int):
        self.entries: dict[str, list[tuple[int, str, int | None]]] = {}
        rows = conn.execute(
            "SELECT row_code, payer, col_index, indicator_id"
            "  FROM indicator_code WHERE tax_code=? AND year=?",
            (tax_code, year),
        ).fetchall()
        for row in rows:
            self.entries.setdefault(row["row_code"].lstrip("0") or "0", []).append(
                (row["indicator_id"], row["payer"], row["col_index"])
            )
            self.entries.setdefault(row["row_code"], []).append(
                (row["indicator_id"], row["payer"], row["col_index"])
            )
        self.available = bool(rows)

    def get(self, code: str) -> list[tuple[int, str, int | None]]:
        entries = self.entries.get(code) or self.entries.get(code.lstrip("0") or "0") or []
        # убираем дубликаты, появившиеся из-за двух вариантов записи кода
        seen: set[tuple[int, str, int | None]] = set()
        unique = []
        for entry in entries:
            if entry not in seen:
                seen.add(entry)
                unique.append(entry)
        return unique


def guess_year(path: Path, title_rows: Sequence[Sequence[str]] = ()) -> int | None:
    for part in (path.stem, *reversed(path.parts[:-1])):
        year = parse_year(part)
        if year and 2000 <= year <= 2100:
            return year
    for row in title_rows:
        for cell in row:
            text = clean(cell)
            if "год" in text.lower() or "период" in text.lower():
                year = parse_year(text)
                if year:
                    return year
    for row in title_rows:
        for cell in row:
            year = parse_year(cell)
            if year:
                return year
    return None


def guess_region(
    path: Path, resolver: RegionResolver, title_rows: Sequence[Sequence[str]] = ()
) -> str | None:
    """Определяет регион по имени файла, каталогу и титульным строкам.

    Год из имени сначала вырезается, иначе «5tn_2019_50.xls» опознаётся как
    регион 20. Двузначный код ищется только как самостоятельный токен.
    """
    year = guess_year(path)
    for part in (path.stem, *reversed(path.parts[:-1])):
        text = part.replace(str(year), " ") if year else part
        for token in re.findall(r"(?<!\d)\d{2}(?!\d)", text):
            if token in resolver.known and token != "00":
                return token
        resolved = resolver.resolve(name=part.replace("_", " ").replace("-", " "))
        if resolved:
            return resolved
    for row in title_rows:
        for cell in row:
            text = clean(cell)
            if len(text) < 4:
                continue
            resolved = resolver.resolve(name=re.sub(r"^(по|в)\s+", "", text, flags=re.I))
            if resolved:
                return resolved
    return None


def _numeric_cells(row: Sequence[str], start: int) -> list[float | None]:
    out: list[float | None] = []
    for cell in row[start + 1:]:
        text = clean(cell)
        if not text:
            out.append(None)
            continue
        out.append(parse_number(text))
    return out


def load_file(
    conn: sqlite3.Connection,
    path: Path | str,
    *,
    tax_code: str = "tn",
    year: int | None = None,
    region_code: str | None = None,
    replace: bool = True,
) -> FormStats:
    """Загружает один файл формы (обычно = один регион за один год)."""
    path = Path(path)
    stats = FormStats(file=path.name, tax_code=tax_code)
    resolver = RegionResolver(conn)

    # строки каждой таблицы читаем сразу: источник потоковый, и переход к
    # следующему листу закрывает предыдущий
    buffered_tables = [
        (table.name, list(table.rows)) for table in tablestream.iter_tables(path)
    ]
    if not buffered_tables:
        stats.problems.append("в файле не найдено таблиц")
        return stats
    title_rows: list[list[str]] = []
    for _name, rows in buffered_tables:
        title_rows.extend(rows[:MAX_TITLE_ROWS])

    stats.year = year or guess_year(path, title_rows)
    stats.region_code = region_code or guess_region(path, resolver, title_rows)
    if stats.year is None:
        stats.problems.append("не удалось определить год (укажите --year)")
        return stats
    if stats.region_code is None:
        stats.problems.append("не удалось определить регион (укажите --region)")
        return stats

    code_map = CodeMap(conn, tax_code, stats.year)
    if not code_map.available:
        stats.problems.append(
            f"нет карты кодов строк для {tax_code} за {stats.year} — "
            f"загрузите методичку командой load-methodology"
        )
        return stats

    payload: dict[tuple[int, str], float] = {}
    for _name, rows in buffered_tables:
        for row in rows:
            stats.rows_read += 1
            cells = [clean(c) for c in row]
            code_idx = _find_code_index(cells, code_map)
            if code_idx is None:
                continue
            entries = code_map.get(cells[code_idx])
            numbers = _numeric_cells(cells, code_idx)
            filled = [n for n in numbers if n is not None]
            for indicator_id, payer, col_index in entries:
                if col_index:                      # старые формы: графа 1 = ЮЛ, 2 = ФЛ
                    if len(filled) >= col_index:
                        payload[(indicator_id, payer)] = filled[col_index - 1]
                elif filled:
                    payload[(indicator_id, payer)] = filled[0]

    if not payload:
        stats.problems.append("в файле не найдено ни одного известного кода строки")
        for _name, rows in buffered_tables:
            for row in rows:
                for cell in row:
                    text = clean(cell)
                    if CODE_RE.match(text) and not code_map.get(text):
                        stats.unknown_codes.add(text)
        return stats

    with conn:
        if replace:
            conn.execute(
                "DELETE FROM fact_form WHERE year=? AND region_code=? AND tax_code=?",
                (stats.year, stats.region_code, tax_code),
            )
        conn.executemany(
            """INSERT INTO fact_form (year, region_code, tax_code, indicator_id, payer,
                                      value, source_file)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (year, region_code, tax_code, indicator_id, payer)
               DO UPDATE SET value=excluded.value, source_file=excluded.source_file""",
            [
                (stats.year, stats.region_code, tax_code, indicator_id, payer, value, path.name)
                for (indicator_id, payer), value in payload.items()
            ],
        )
    stats.values = len(payload)
    db.log_load(conn, "forms", str(path), stats.rows_read, stats.values,
                f"{tax_code} {stats.year} {stats.region_code}")
    return stats


def _find_code_index(cells: Sequence[str], code_map: CodeMap) -> int | None:
    """Ищет ячейку с кодом строки формы (графа «Б»)."""
    for idx, cell in enumerate(cells):
        text = clean(cell)
        if not CODE_RE.match(text):
            # xls нередко отдаёт код как число: 1100.0
            if re.match(r"^\d{3,4}\.0$", text):
                text = text[:-2]
            else:
                continue
        if code_map.get(text):
            return idx
    return None


def load_directory(
    conn: sqlite3.Connection,
    directory: Path | str,
    *,
    tax_code: str = "tn",
    year: int | None = None,
    pattern: str = "*",
    on_file: object | None = None,
) -> list[FormStats]:
    directory = Path(directory)
    results: list[FormStats] = []
    files = sorted(
        p for p in directory.rglob(pattern)
        if p.is_file() and p.suffix.lower() in tablestream.TABLE_SUFFIXES | {".zip", ".gz"}
    )
    for path in files:
        stats = load_file(conn, path, tax_code=tax_code, year=year)
        results.append(stats)
        if on_file:
            on_file(stats)
    return results


def recompute_totals(conn: sqlite3.Connection, tax_code: str | None = None) -> int:
    """Добавляет строки payer='total' = ЮЛ + ФЛ (удобно для сводных графиков)."""
    params: tuple = ()
    where = ""
    if tax_code:
        where = " WHERE tax_code = ?"
        params = (tax_code,)
    with conn:
        conn.execute(f"DELETE FROM fact_form WHERE payer='total'{' AND tax_code=?' if tax_code else ''}", params)
        cur = conn.execute(
            f"""INSERT INTO fact_form (year, region_code, tax_code, indicator_id, payer, value, source_file)
                SELECT year, region_code, tax_code, indicator_id, 'total', SUM(value), 'расчёт'
                  FROM fact_form
                 {where + (' AND' if where else ' WHERE')} payer IN ('ul','fl') AND value IS NOT NULL
                 GROUP BY year, region_code, tax_code, indicator_id""",
            params,
        )
        return cur.rowcount
