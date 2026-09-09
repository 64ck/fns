"""Автоматический импорт файлов из рабочей папки.

Сценарий: пользователь кладёт выгрузки ФНС рядом с программой и запускает её.
Модуль сам обходит папку, определяет по содержимому, что за файл (ставки и
льготы или форма статотчётности), загружает новые файлы и запоминает их,
чтобы при следующем запуске не грузить то же самое повторно.
"""
from __future__ import annotations

import re
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import analytics, config, db
from .sources import forms, rates, tablestream

SUPPORTED = tablestream.TABLE_SUFFIXES | {".zip", ".gz"}
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "_internal",
    "reference", "portal", "fnsportal", "tests", "db", "node_modules",
}
CODE_RE = re.compile(r"^\d{3,4}$")
MAX_PROBE_ROWS = 3000
HEADER_LOOKAHEAD = 5000
# Форма отчётности — узкая таблица, где коды строк стоят в одной и той же
# графе. Без этих условий обычные числа в данных (100, 110, 120) выглядели бы
# как коды строк, и выгрузка ставок опознавалась бы как форма.
FORM_MAX_WIDTH = 12
FORM_MIN_CODES = 5
PROGRESS_EVERY_SEC = 15


@dataclass
class ImportResult:
    imported: list[tuple[str, str, str]] = field(default_factory=list)   # файл, тип, итог
    skipped: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.imported)

    def as_dict(self) -> dict:
        return {
            "imported": self.imported, "skipped": len(self.skipped),
            "unknown": self.unknown, "failed": self.failed,
        }


def candidates(base: Path) -> Iterator[Path]:
    """Файлы рабочей папки, которые имеет смысл пробовать импортировать."""
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(base).parts[:-1]):
            continue
        if path.suffix.lower() not in SUPPORTED:
            continue
        if path.name.startswith((".", "~$")):
            continue
        yield path


def _known_codes(conn: sqlite3.Connection) -> dict[str, set[str]]:
    codes: dict[str, set[str]] = {}
    for row in conn.execute("SELECT DISTINCT tax_code, row_code FROM indicator_code"):
        codes.setdefault(row["tax_code"], set()).add(row["row_code"])
    return codes


def classify(
    conn: sqlite3.Connection, path: Path, codes: dict[str, set[str]] | None = None
) -> tuple[str, str, str | None]:
    """Определяет тип файла по содержимому.

    Возвращает (тип, пояснение, налог): тип — 'rates', 'forms' или 'unknown';
    пояснение показывается пользователю; налог заполняется для форм — по тому,
    коды строк какого налога нашлись в файле.
    """
    codes = codes if codes is not None else _known_codes(conn)
    aliases = rates.load_aliases()
    try:
        for table in tablestream.iter_tables(path):
            probe: list[list[str]] = []
            idx, header, buffered = tablestream.find_header(
                table.rows, required=rates.HEADER_MARKERS, lookahead=HEADER_LOOKAHEAD)
            probe.extend(buffered)
            if header:
                mapping = rates.map_columns(header, aliases)
                has_region = any(mapping.get(key) for key in ("region_code", "region_name", "oktmo"))
                has_norm = any(mapping.get(key) for key in
                               ("rate_value", "benefit_category", "benefit_kind", "benefit_size"))
                if has_region and (has_norm or len(mapping) >= 5):
                    found = ", ".join(sorted(mapping)[:6])
                    return "rates", f"распознаны колонки: {found}…", None
            # не похоже на ставки — ищем коды строк формы
            for row in table.rows:
                probe.append(row)
                if len(probe) >= MAX_PROBE_ROWS:
                    break
            for tax_code, tax_codes in codes.items():
                by_column: dict[int, set[str]] = {}
                for row in probe:
                    if len(row) > FORM_MAX_WIDTH:
                        continue
                    for column, cell in enumerate(row):
                        text = cell.strip()
                        if CODE_RE.match(text) and text in tax_codes:
                            by_column.setdefault(column, set()).add(text)
                for column, hits in sorted(by_column.items()):
                    if len(hits) >= FORM_MIN_CODES:
                        return ("forms",
                                f"коды строк формы ({tax_code}) в графе {column + 1}: "
                                f"{sorted(hits)[:5]}…",
                                tax_code)
            break   # достаточно первой таблицы
    except Exception as error:  # noqa: BLE001 — файл может быть битым
        return "unknown", f"не удалось прочитать: {error}", None
    return "unknown", "не похоже ни на ставки/льготы, ни на форму отчётности", None


def _seen(conn: sqlite3.Connection, base: Path, path: Path) -> bool:
    key = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
    stat = path.stat()
    row = conn.execute(
        "SELECT size, mtime FROM imported_file WHERE path=?", (key,)).fetchone()
    return bool(row and row["size"] == stat.st_size and abs(row["mtime"] - stat.st_mtime) < 1)


def _remember(conn: sqlite3.Connection, base: Path, path: Path, kind: str, details: str) -> None:
    key = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
    stat = path.stat()
    with conn:
        conn.execute(
            """INSERT INTO imported_file (path, size, mtime, kind, details)
               VALUES (?,?,?,?,?)
               ON CONFLICT (path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
                   kind=excluded.kind, details=excluded.details,
                   loaded_at=datetime('now')""",
            (key, stat.st_size, stat.st_mtime, kind, details[:500]))


def run(
    conn: sqlite3.Connection,
    base: Path | None = None,
    *,
    default_tax: str = "tn",   # если налог не определился по кодам строк
    rebuild_terms: bool = True,
    log: Callable[[str], None] = print,
) -> ImportResult:
    """Обходит рабочую папку и импортирует всё новое."""
    base = Path(base or config.ROOT)
    result = ImportResult()
    codes = _known_codes(conn)
    taxes_touched: set[str] = set()
    benefits_added = False

    files = list(candidates(base))
    if not files:
        log(f"В папке {base} нет файлов данных (csv, xls, xlsx, html, zip).")
        return result

    for path in files:
        if _seen(conn, base, path):
            result.skipped.append(path.name)
            continue
        kind, note, file_tax = classify(conn, path, codes)
        size_mb = path.stat().st_size / 1e6
        if kind == "unknown":
            log(f"  ? {path.name} ({size_mb:.1f} МБ) — пропущен: {note}")
            result.unknown.append(path.name)
            continue
        
        log(f"  → {path.name} ({size_mb:.1f} МБ): {kind}, {note}")
        try:
            if kind == "rates":
                started = time.time()
                last_shown = [started]

                def show(stats, read_bytes, total_bytes, started=started, log=log):
                    now = time.time()
                    if now - last_shown[0] < PROGRESS_EVERY_SEC:
                        return          # не заваливаем консоль на многочасовой загрузке
                    last_shown[0] = now
                    elapsed = now - started
                    share = read_bytes / total_bytes if total_bytes else 0
                    left = (elapsed / share - elapsed) if share > 0.01 else 0
                    log(f"     {share * 100:5.1f}% | строк: {stats.rows_read:>12,} | "
                        f"прошло {elapsed / 60:4.0f} мин, осталось ~{left / 60:.0f} мин"
                        .replace(",", " "))

                stats = rates.load_file(conn, path, progress=show if size_mb > 50 else None)
                summary = (f"ставок {stats.rates}, льгот {stats.benefits}"
                           f" из {stats.rows_read} строк")
                benefits_added = benefits_added or stats.benefits > 0
                if stats.unmapped_regions:
                    log(f"     не опознаны регионы: {sorted(stats.unmapped_regions)[:5]}")
                if stats.unmapped_taxes:
                    log(f"     не опознаны налоги: {sorted(stats.unmapped_taxes)[:5]}")
            else:
                tax_code = file_tax or default_tax
                stats = forms.load_file(conn, path, tax_code=tax_code)
                if stats.problems:
                    log(f"     ! {'; '.join(stats.problems)}")
                    result.failed.append((path.name, "; ".join(stats.problems)))
                    continue
                summary = f"{stats.region_code} / {stats.year}: {stats.values} значений"
                taxes_touched.add(tax_code)
        except Exception as error:  # noqa: BLE001 — один плохой файл не должен ронять запуск
            log(f"     ! ошибка: {error}")
            result.failed.append((path.name, traceback.format_exc(limit=2)))
            continue
        log(f"     ✓ {summary}")
        result.imported.append((path.name, kind, summary))
        _remember(conn, base, path, kind, summary)

    if result.unknown:
        log("\n  Не распознаны файлы: " + ", ".join(result.unknown[:10]))
        log("  Запустите программу с ключом --inspect — она покажет их структуру")
        log("  и сохранит отчёт inspect-report.txt рядом с собой.")

    for tax_code in taxes_touched:
        inserted = forms.recompute_totals(conn, tax_code)
        log(f"  рассчитано строк «всего» (ЮЛ+ФЛ): {inserted}")
    if benefits_added and rebuild_terms:
        log("  строю частотный словарь для облаков слов…")
        info = analytics.build_terms(conn)
        log(f"  словарь готов: {info['terms_kept']} терминов")
    return result
