"""Отчёт о структуре исходных файлов.

Нужен, когда программа не смогла распознать файл: отчёт показывает, что
именно она в нём видит — кодировку, начало разметки, строки таблиц, самые
«широкие» строки (обычно это и есть шапка данных) и то, какие колонки
удалось сопоставить с полями портала. По такому отчёту достаточно дописать
пару строк в reference/column_aliases.json, чтобы файл начал загружаться.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator, Sequence

from . import config
from .sources import rates, tablestream
from .textutil import clean

RAW_PREVIEW = 3000        # сколько символов исходной разметки показать
SCAN_ROWS = 2000          # сколько строк просмотреть для статистики
SHOW_ROWS = 40            # сколько строк показать целиком
CELL_LIMIT = 60           # обрезка длинных ячеек


def _raw_head(path: Path, limit: int = RAW_PREVIEW) -> tuple[str, str]:
    """Начало файла в текстовом виде и определённая кодировка."""
    try:
        with tablestream._open_binary(path) as raw:
            head = raw.read(max(limit * 4, 64 * 1024))
    except Exception as error:  # noqa: BLE001
        return "", f"не удалось прочитать: {error}"
    encoding = tablestream.detect_encoding(head)
    return head.decode(encoding, "replace")[:limit], encoding


def _row_line(index: int, row: Sequence[str]) -> str:
    cells = [clean(cell)[:CELL_LIMIT] for cell in row]
    while cells and not cells[-1]:
        cells.pop()
    return f"    [{index:>5}] ({len(row):>3} яч.) " + " | ".join(cells)[:400]


def describe_file(path: Path, conn: sqlite3.Connection | None = None) -> str:
    """Собирает текстовый отчёт по одному файлу."""
    lines: list[str] = []
    size_mb = path.stat().st_size / 1e6
    lines.append("=" * 78)
    lines.append(f"ФАЙЛ: {path}")
    lines.append(f"  размер: {size_mb:.1f} МБ, расширение: {path.suffix or '(нет)'}")

    if path.suffix.lower() in {".htm", ".html", ".xml", ".csv", ".txt", ".tsv"}:
        head, encoding = _raw_head(path)
        lines.append(f"  кодировка (определена): {encoding}")
        lines.append("  начало файла (как есть):")
        for chunk in head.splitlines()[:40]:
            if chunk.strip():
                lines.append(f"    {chunk.strip()[:200]}")

    aliases = rates.load_aliases()
    try:
        tables = tablestream.iter_tables(path)
    except Exception as error:  # noqa: BLE001
        lines.append(f"  ! не удалось открыть: {error}")
        return "\n".join(lines)

    for table_index, table in enumerate(tables):
        if table_index >= 3:
            lines.append("  … остальные таблицы пропущены")
            break
        lines.append(f"\n  ТАБЛИЦА «{table.name}»")
        rows: list[list[str]] = []
        widths: dict[int, int] = {}
        for row in table.rows:
            rows.append(row)
            widths[len(row)] = widths.get(len(row), 0) + 1
            if len(rows) >= SCAN_ROWS:
                break
        if not rows:
            lines.append("    строк не найдено")
            continue

        lines.append(f"    просмотрено строк: {len(rows)}")
        top_widths = sorted(widths.items(), key=lambda item: -item[1])[:6]
        lines.append("    распределение числа ячеек в строке: "
                     + ", ".join(f"{width} яч. × {count}" for width, count in top_widths))

        lines.append("    первые строки:")
        shown = 0
        for index, row in enumerate(rows):
            if not any(clean(cell) for cell in row):
                continue
            lines.append(_row_line(index, row))
            shown += 1
            if shown >= SHOW_ROWS:
                break

        widest = max(rows, key=len)
        lines.append(f"    самая широкая строка ({len(widest)} ячеек):")
        lines.append(_row_line(rows.index(widest), widest))

        # кандидаты в шапку: строки с большим числом текстовых ячеек
        best: list[tuple[int, int, list[str]]] = []
        for index, row in enumerate(rows):
            filled = [clean(cell) for cell in row if clean(cell)]
            if len(filled) < 3:
                continue
            texty = sum(1 for cell in filled if not cell.replace(",", "").replace(".", "").isdigit())
            if texty >= 3:
                mapping = rates.map_columns(row, aliases)
                best.append((len(mapping), index, row))
        best.sort(key=lambda item: (-item[0], item[1]))
        if best:
            lines.append("    кандидаты в шапку (сколько полей портала распознано):")
            for matched, index, row in best[:5]:
                mapping = rates.map_columns(row, aliases)
                lines.append(f"      строка {index}: распознано {matched}")
                lines.append(_row_line(index, row))
                for field_name, column in sorted(mapping.items()):
                    lines.append(f"          {field_name:18} <- {column[:60]}")
                unmapped = [clean(c) for c in row if clean(c) and c not in mapping.values()]
                if unmapped:
                    lines.append(f"          не распознаны: {unmapped[:12]}")
        else:
            lines.append("    строк, похожих на шапку, не найдено")

        if conn is not None:
            from .autoload import classify

            kind, note, tax = classify(conn, path)
            lines.append(f"    вывод программы: {kind} — {note}"
                         + (f" (налог: {tax})" if tax else ""))
            break
    return "\n".join(lines)


def report(paths: Sequence[Path], conn: sqlite3.Connection | None = None) -> str:
    header = [
        "ОТЧЁТ О СТРУКТУРЕ ИСХОДНЫХ ФАЙЛОВ",
        f"рабочая папка: {config.ROOT}",
        f"файлов на проверку: {len(paths)}",
        "",
    ]
    body = [describe_file(path, conn) for path in paths]
    return "\n".join(header + body)
