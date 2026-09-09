"""Потоковое чтение табличных исходников ФНС: csv, xls, xlsx, html, zip, gz.

Особенности данных ФНС, которые здесь учтены:
  * выгрузка ставок и льгот в HTML весит порядка 3 ГБ — файл читается
    чанками, в память попадает одна строка таблицы, а не документ целиком;
  * кодировка csv/html бывает windows-1251, разделитель — ';' или ',';
  * формы статотчётности приходят в старом .xls (BIFF) и в .xlsx;
  * архивы .zip с одним или несколькими файлами внутри.
"""
from __future__ import annotations

import csv
import gzip
import html as html_mod
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from ..textutil import clean, norm_key

CHUNK = 1 << 20            # 1 МБ
MAX_ROW_BUFFER = 32 << 20  # предохранитель от «строки» на пол-файла
ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1")

TABLE_SUFFIXES = {".csv", ".txt", ".tsv", ".xls", ".xlsx", ".xlsm", ".htm", ".html", ".xml"}


@dataclass
class Table:
    """Одна таблица внутри источника (лист книги или <table> в HTML).

    Строки читаются лениво, поэтому `rows` нужно прочитать до перехода
    к следующей таблице: источник (книга xlsx/xls) закрывается по мере обхода.
    """

    name: str
    rows: Iterator[list[str]]


# ------------------------------------------------------------------ кодировки

def detect_encoding(sample: bytes) -> str:
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    m = re.search(rb"charset\s*=\s*[\"']?\s*([\w-]+)", sample[:8192], re.I)
    if m:
        enc = m.group(1).decode("ascii", "ignore").lower()
        if enc in {"windows-1251", "cp1251", "win-1251"}:
            return "cp1251"
        if enc.startswith("utf-8"):
            return "utf-8"
    for enc in ("utf-8", "cp1251"):
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    # доля кириллицы в cp1251 как последний аргумент
    return "cp1251"


def detect_delimiter(sample: str) -> str:
    head = "\n".join(sample.splitlines()[:20])
    counts = {d: head.count(d) for d in (";", "\t", ",", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _open_binary(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rb")
    return path.open("rb")


# ------------------------------------------------------------------------ csv

def iter_csv(path: Path, encoding: str | None = None, delimiter: str | None = None) -> Iterator[list[str]]:
    with _open_binary(path) as raw:
        sample = raw.read(64 * 1024)
    enc = encoding or detect_encoding(sample)
    delim = delimiter or detect_delimiter(sample.decode(enc, "replace"))
    with _open_binary(path) as raw:
        stream = io.TextIOWrapper(raw, encoding=enc, errors="replace", newline="")
        for row in csv.reader(stream, delimiter=delim, quotechar='"'):
            yield [clean(c) for c in row]


# ----------------------------------------------------------------- xls / xlsx

def iter_xlsx(path: Path, sheet: str | int | None = None) -> Iterator[Table]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    sheets = wb.worksheets
    if isinstance(sheet, int):
        sheets = [wb.worksheets[sheet]]
    elif isinstance(sheet, str):
        sheets = [wb[sheet]]

    def rows_of(ws) -> Iterator[list[str]]:
        for row in ws.iter_rows(values_only=True):
            yield [clean(c) for c in row]

    try:
        for ws in sheets:
            yield Table(ws.title, rows_of(ws))
    finally:
        wb.close()


def iter_xls(path: Path, sheet: str | int | None = None) -> Iterator[Table]:
    import xlrd  # xlrd>=2.0 читает именно старый .xls

    book = xlrd.open_workbook(path, on_demand=True)
    names = book.sheet_names()
    if isinstance(sheet, int):
        names = [names[sheet]]
    elif isinstance(sheet, str):
        names = [sheet]

    def rows_of(name: str) -> Iterator[list[str]]:
        ws = book.sheet_by_name(name)
        try:
            for idx in range(ws.nrows):
                yield [clean(v) for v in ws.row_values(idx)]
        finally:
            book.unload_sheet(name)

    try:
        for name in names:
            yield Table(name, rows_of(name))
    finally:
        book.release_resources()


# ----------------------------------------------------------------------- html

_TR_OPEN = re.compile(r"<tr\b", re.I)
_TR_CLOSE = re.compile(r"</tr\s*>", re.I)
_CELL_SPLIT = re.compile(r"<t[dh]\b", re.I)
_COLSPAN = re.compile(r"colspan\s*=\s*[\"']?(\d+)", re.I)
_TAG = re.compile(r"<[^>]*>", re.S)
_BR = re.compile(r"<\s*br\s*/?\s*>|</\s*p\s*>|</\s*div\s*>", re.I)
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)


def _html_cells(row_html: str) -> list[str]:
    parts = _CELL_SPLIT.split(row_html)[1:]
    cells: list[str] = []
    for part in parts:
        head, _, body = part.partition(">")
        span = _COLSPAN.search(head)
        text = _BR.sub(" ", body)
        text = _TAG.sub(" ", text)
        text = html_mod.unescape(text)
        cells.append(clean(text))
        if span:
            cells.extend([""] * (max(1, int(span.group(1))) - 1))
    return cells


def iter_html(path: Path, encoding: str | None = None) -> Iterator[list[str]]:
    """Потоково выдаёт строки всех таблиц HTML-файла любого размера."""
    with _open_binary(path) as raw:
        sample = raw.read(64 * 1024)
    enc = encoding or detect_encoding(sample)

    buffer = ""
    with _open_binary(path) as raw:
        while True:
            chunk = raw.read(CHUNK)
            if not chunk:
                break
            buffer += chunk.decode(enc, "replace")
            buffer = _SCRIPT.sub(" ", buffer)
            while True:
                m_open = _TR_OPEN.search(buffer)
                if not m_open:
                    break
                m_close = _TR_CLOSE.search(buffer, m_open.end())
                if not m_close:
                    break
                yield _html_cells(buffer[m_open.start(): m_close.start()])
                buffer = buffer[m_close.end():]
            if len(buffer) > MAX_ROW_BUFFER:  # мусор без закрывающих тегов
                buffer = buffer[-CHUNK:]
    tail_open = _TR_OPEN.search(buffer)
    if tail_open:
        yield _html_cells(buffer[tail_open.start():])


# ------------------------------------------------------------- единая точка входа

def iter_tables(path: Path | str, sheet: str | int | None = None) -> Iterator[Table]:
    """Возвращает таблицы источника; строки читаются лениво."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".gz":
        suffix = Path(path.stem).suffix.lower()

    if suffix == ".zip":
        yield from _iter_zip(path, sheet)
    elif suffix in {".xlsx", ".xlsm"}:
        yield from iter_xlsx(path, sheet)
    elif suffix == ".xls":
        yield from iter_xls(path, sheet)
    elif suffix in {".htm", ".html", ".xml"}:
        yield Table(path.name, iter_html(path))
    else:
        yield Table(path.name, iter_csv(path))


def _iter_zip(path: Path, sheet: str | int | None) -> Iterator[Table]:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            inner = Path(info.filename)
            if inner.suffix.lower() not in TABLE_SUFFIXES:
                continue
            tmp = path.parent / f".{path.stem}__{inner.name}"
            with zf.open(info) as src, tmp.open("wb") as dst:
                while True:
                    block = src.read(CHUNK)
                    if not block:
                        break
                    dst.write(block)
            try:
                for table in iter_tables(tmp, sheet):
                    yield Table(f"{inner.name}:{table.name}", table.rows)
            finally:
                tmp.unlink(missing_ok=True)


def iter_rows(path: Path | str, sheet: str | int | None = None) -> Iterator[list[str]]:
    for table in iter_tables(path, sheet):
        yield from table.rows


# ------------------------------------------------------- заголовки и записи

def find_header(rows: Iterable[list[str]], required: Sequence[str] = (), lookahead: int = 40):
    """Ищет строку заголовка среди первых `lookahead` строк источника.

    В выгрузках ФНС над таблицей часто идут титульные строки, поэтому берётся
    строка с максимальным числом совпадений с ожидаемыми словами (`required`)
    и максимальной долей текстовых ячеек. При равном счёте побеждает более
    ранняя строка.

    Возвращает (индекс_заголовка, заголовок, прочитанные_строки).
    """
    buffer: list[list[str]] = []
    need = [norm_key(r) for r in required if norm_key(r)]
    threshold = max(1, (len(need) + 1) // 2) if need else 0
    best: tuple[int, float, int, list[str]] | None = None

    for row in rows:
        buffer.append(row)
        index = len(buffer) - 1
        filled = [c for c in row if clean(c)]
        if len(filled) < 2:
            if len(buffer) >= lookahead:
                break
            continue
        keys = [norm_key(c) for c in row]
        score = sum(1 for n in need if any(n in k for k in keys))
        texty = sum(
            1 for c in filled
            if not c.replace(",", "").replace(".", "").replace(" ", "").isdigit()
        ) / len(filled)
        acceptable = score >= threshold if need else texty >= 0.75
        if acceptable and (best is None or (score, texty) > (best[0], best[1])):
            best = (score, texty, index, row)
        if need and best is not None and best[0] >= len(need):
            break
        if len(buffer) >= lookahead:
            break

    if best is None:
        return None, [], buffer
    return best[2], best[3], buffer


def iter_records(
    path: Path | str,
    required: Sequence[str] = (),
    sheet: str | int | None = None,
) -> Iterator[dict[str, str]]:
    """Читает источник как набор словарей «заголовок -> значение»."""
    for table in iter_tables(path, sheet):
        rows = table.rows
        idx, header, buffered = find_header(rows, required)
        if not header or idx is None:
            continue
        names = _dedupe(header)
        for row in buffered[idx + 1:]:
            yield _to_record(names, row, table.name)
        for row in rows:
            yield _to_record(names, row, table.name)


def _dedupe(header: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for cell in header:
        name = clean(cell) or "col"
        if name in seen:
            seen[name] += 1
            name = f"{name}__{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def _to_record(names: list[str], row: list[str], table_name: str) -> dict[str, str]:
    record = {names[i]: clean(v) for i, v in enumerate(row) if i < len(names)}
    record["__table__"] = table_name
    return record


def describe(path: Path | str, max_rows: int = 8) -> list[dict]:
    """Структура источника: таблицы, размеры, первые строки (для команды inspect)."""
    out = []
    for table in iter_tables(path):
        preview: list[list[str]] = []
        widths: list[int] = []
        for i, row in enumerate(table.rows):
            if i < max_rows:
                preview.append(row)
            widths.append(len(row))
            if i > 500:
                break
        idx, header, _ = find_header(iter(preview))
        out.append({
            "table": table.name,
            "sampled_rows": len(widths),
            "max_width": max(widths) if widths else 0,
            "header_row": idx,
            "header": header,
            "preview": preview,
        })
    return out
