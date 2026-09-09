"""Схема SQLite и работа с подключением.

Модель данных построена вокруг трёх измерений, по которым связываются
оба набора данных ФНС: год, регион и налог.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from . import config

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- ------------------------------------------------------------------ измерения
CREATE TABLE IF NOT EXISTS region (
    code             TEXT PRIMARY KEY,   -- код региона ФНС ('77', '50', '00' = РФ)
    name             TEXT NOT NULL,
    short_name       TEXT,
    federal_district TEXT,
    kind             TEXT DEFAULT 'subject',  -- subject | aggregate
    valid_from       INTEGER,
    valid_to         INTEGER,
    note             TEXT
);

CREATE TABLE IF NOT EXISTS tax (
    code       TEXT PRIMARY KEY,   -- 'tn', 'zn', 'nifl', 'niul'
    name       TEXT NOT NULL,
    short_name TEXT,
    level      TEXT,               -- regional | local | federal
    form_code  TEXT,               -- код формы статотчётности, напр. '5-ТН'
    sort_order INTEGER DEFAULT 100
);

-- Показатель формы статотчётности в каноническом (сквозном по годам) виде
CREATE TABLE IF NOT EXISTS indicator (
    id          INTEGER PRIMARY KEY,
    tax_code    TEXT NOT NULL REFERENCES tax(code),
    slug        TEXT NOT NULL,      -- нормализованный ключ показателя
    name        TEXT NOT NULL,
    section     TEXT,               -- номер раздела формы ('1'..'5')
    unit        TEXT,               -- 'единиц', 'тыс. руб.'
    parent_slug TEXT,
    level       INTEGER DEFAULT 0,
    sort_order  INTEGER DEFAULT 0,
    UNIQUE (tax_code, slug)
);

-- Карта «год формы -> код строки -> показатель». Формы меняются, коды строк
-- переезжают, поэтому маппинг хранится отдельно (методичка по 5-ТН).
CREATE TABLE IF NOT EXISTS indicator_code (
    tax_code     TEXT NOT NULL,
    year         INTEGER NOT NULL,
    payer        TEXT NOT NULL,     -- 'ul' | 'fl'
    row_code     TEXT NOT NULL,
    col_index    INTEGER,           -- номер графы (для форм 2006 г. и ранее)
    indicator_id INTEGER NOT NULL REFERENCES indicator(id),
    PRIMARY KEY (tax_code, year, payer, row_code)
);

-- ------------------------------------------------------------- факты (формы)
CREATE TABLE IF NOT EXISTS fact_form (
    year         INTEGER NOT NULL,
    region_code  TEXT NOT NULL,
    tax_code     TEXT NOT NULL,
    indicator_id INTEGER NOT NULL REFERENCES indicator(id),
    payer        TEXT NOT NULL,     -- 'ul' | 'fl' | 'total'
    value        REAL,
    source_file  TEXT,
    PRIMARY KEY (year, region_code, tax_code, indicator_id, payer)
);
CREATE INDEX IF NOT EXISTS ix_fact_dim ON fact_form (tax_code, year, indicator_id, payer);
CREATE INDEX IF NOT EXISTS ix_fact_region ON fact_form (region_code, tax_code, year);

-- ------------------------------------------- нормативные данные: ставки/льготы
CREATE TABLE IF NOT EXISTS rate (
    id            INTEGER PRIMARY KEY,
    year          INTEGER,            -- налоговый период (если указан явно)
    year_from     INTEGER,            -- период действия нормы, с
    year_to       INTEGER,            -- период действия нормы, по (NULL = бессрочно)
    region_code   TEXT,
    tax_code      TEXT,
    oktmo         TEXT,
    mo_name       TEXT,
    payer         TEXT,             -- основная категория: fl | ip | ul | all
    -- Выгрузка ФНС помечает каждую ставку и льготу категориями, к которым она
    -- относится, и их может быть несколько. Поэтому фильтрация идёт по флагам,
    -- а не по одному полю payer.
    for_fl        INTEGER DEFAULT 0,
    for_ul        INTEGER DEFAULT 0,
    for_ip        INTEGER DEFAULT 0,
    payer_text    TEXT,             -- исходная формулировка «плательщик»
    object_name   TEXT,             -- объект налогообложения
    object_group  TEXT,             -- группа объекта («Автомобили легковые…»)
    rate_value    REAL,
    rate_text     TEXT,
    rate_unit     TEXT,
    condition     TEXT,
    npa_name      TEXT,
    npa_number    TEXT,
    npa_date      TEXT,
    npa_authority TEXT,
    period_from   TEXT,
    period_to     TEXT,
    source_file   TEXT
);
CREATE INDEX IF NOT EXISTS ix_rate_dim ON rate (tax_code, year, region_code);
CREATE INDEX IF NOT EXISTS ix_rate_payer ON rate (tax_code, year, region_code, payer);
CREATE INDEX IF NOT EXISTS ix_rate_period ON rate (tax_code, region_code, year_from, year_to);
CREATE INDEX IF NOT EXISTS ix_rate_flags ON rate (tax_code, region_code, for_fl, for_ul, for_ip);

CREATE TABLE IF NOT EXISTS benefit (
    id            INTEGER PRIMARY KEY,
    year          INTEGER,
    year_from     INTEGER,
    year_to       INTEGER,
    region_code   TEXT,
    tax_code      TEXT,
    oktmo         TEXT,
    mo_name       TEXT,
    payer         TEXT,             -- основная категория: fl | ip | ul | all
    for_fl        INTEGER DEFAULT 0,
    for_ul        INTEGER DEFAULT 0,
    for_ip        INTEGER DEFAULT 0,
    category      TEXT,             -- категория налогоплательщика (текст льготы)
    kind          TEXT,             -- вид льготы (освобождение / пониженная ставка / вычет)
    size_text     TEXT,
    size_value    REAL,
    size_unit     TEXT,
    condition     TEXT,
    basis         TEXT,             -- основание (статья НПА)
    npa_name      TEXT,
    npa_number    TEXT,
    npa_date      TEXT,
    npa_authority TEXT,
    period_from   TEXT,
    period_to     TEXT,
    source_file   TEXT
);
CREATE INDEX IF NOT EXISTS ix_benefit_dim ON benefit (tax_code, year, region_code);
CREATE INDEX IF NOT EXISTS ix_benefit_payer ON benefit (tax_code, year, region_code, payer);
CREATE INDEX IF NOT EXISTS ix_benefit_period ON benefit (tax_code, region_code, year_from, year_to);
CREATE INDEX IF NOT EXISTS ix_benefit_flags ON benefit (tax_code, region_code, for_fl, for_ul, for_ip);

-- Частотный словарь по льготам для облаков слов.
-- Считается один раз командой `build-terms`, поэтому облако строится
-- мгновенно даже на миллионах строк льгот.
CREATE TABLE IF NOT EXISTS benefit_term (
    tax_code    TEXT NOT NULL,
    year        INTEGER NOT NULL,
    region_code TEXT NOT NULL,
    payer       TEXT NOT NULL,
    term        TEXT NOT NULL,      -- нормализованная (стеммированная) форма
    display     TEXT NOT NULL,      -- самая частая словоформа для показа
    is_bigram   INTEGER DEFAULT 0,
    term_freq   INTEGER NOT NULL,   -- всего вхождений
    doc_freq    INTEGER NOT NULL,   -- в скольких льготах встретилось
    PRIMARY KEY (tax_code, year, region_code, payer, term)
);
CREATE INDEX IF NOT EXISTS ix_term_lookup ON benefit_term (tax_code, payer, year, region_code);

-- ---------------------------------------------------------------- служебные
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Что уже импортировано из рабочей папки: повторный запуск не грузит то же дважды
CREATE TABLE IF NOT EXISTS imported_file (
    path      TEXT PRIMARY KEY,
    size      INTEGER,
    mtime     REAL,
    kind      TEXT,
    loaded_at TEXT DEFAULT (datetime('now')),
    details   TEXT
);

CREATE TABLE IF NOT EXISTS load_log (
    id          INTEGER PRIMARY KEY,
    loaded_at   TEXT DEFAULT (datetime('now')),
    kind        TEXT,      -- rates | benefits | forms | methodology | demo
    source_file TEXT,
    rows_read   INTEGER,
    rows_saved  INTEGER,
    details     TEXT
);
"""


# Столбцы, появившиеся после первой версии схемы. У пользователя уже может
# лежать база, созданная прошлой версией программы: CREATE TABLE IF NOT EXISTS
# её не тронет, поэтому недостающие столбцы добавляются отдельно — иначе
# создание индексов по ним падает с «no such column».
ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "rate": (
        ("year_from", "INTEGER"), ("year_to", "INTEGER"),
        ("for_fl", "INTEGER DEFAULT 0"), ("for_ul", "INTEGER DEFAULT 0"),
        ("for_ip", "INTEGER DEFAULT 0"), ("object_group", "TEXT"),
    ),
    "benefit": (
        ("year_from", "INTEGER"), ("year_to", "INTEGER"),
        ("for_fl", "INTEGER DEFAULT 0"), ("for_ul", "INTEGER DEFAULT 0"),
        ("for_ip", "INTEGER DEFAULT 0"),
    ),
}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Дотягивает старую базу до текущей схемы. Возвращает список изменений."""
    applied: list[str] = []
    tables = {
        row["name"] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table, columns in ADDED_COLUMNS.items():
        if table not in tables:
            continue
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        added = []
        for name, declaration in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                added.append(name)
        if any(name.startswith("for_") for name in added):
            # у старых записей категория хранилась одним полем payer
            conn.execute(
                f"""UPDATE {table}
                       SET for_fl = CASE WHEN payer='fl' THEN 1 ELSE 0 END,
                           for_ul = CASE WHEN payer='ul' THEN 1 ELSE 0 END,
                           for_ip = CASE WHEN payer='ip' THEN 1 ELSE 0 END
                     WHERE payer IN ('fl','ul','ip')""")
        applied += [f"{table}.{name}" for name in added]
    if applied:
        conn.commit()
    return applied


def connect(path: Path | str | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if readonly and db_path.exists():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str | None = None, report=None) -> Path:
    db_path = Path(path or config.DB_PATH)
    conn = connect(db_path)
    changes = migrate(conn)          # до создания индексов по новым столбцам
    if changes and report:
        report(f"структура базы обновлена: {', '.join(changes)}")
    with conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    conn.close()
    return db_path


@contextmanager
def bulk_insert(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Быстрая пакетная загрузка: временно ослабляем гарантии записи."""
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -200000")  # ~200 МБ страничного кэша
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA synchronous = NORMAL")


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[Sequence]) -> int:
    cur = conn.executemany(sql, rows)
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def log_load(
    conn: sqlite3.Connection,
    kind: str,
    source_file: str,
    rows_read: int,
    rows_saved: int,
    details: str = "",
) -> None:
    conn.execute(
        "INSERT INTO load_log(kind, source_file, rows_read, rows_saved, details)"
        " VALUES (?,?,?,?,?)",
        (kind, source_file, rows_read, rows_saved, details),
    )
    conn.commit()
