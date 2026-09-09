"""Обновление базы, созданной прошлой версией программы."""
from __future__ import annotations

import sqlite3

from fnsportal import analytics, db

# Схема из версии, в которой ещё не было флагов категорий и группы объекта
OLD_SCHEMA = """
CREATE TABLE rate (
    id INTEGER PRIMARY KEY, year INTEGER, year_from INTEGER, year_to INTEGER,
    region_code TEXT, tax_code TEXT, oktmo TEXT, mo_name TEXT, payer TEXT,
    payer_text TEXT, object_name TEXT, rate_value REAL, rate_text TEXT,
    rate_unit TEXT, condition TEXT, npa_name TEXT, npa_number TEXT, npa_date TEXT,
    npa_authority TEXT, period_from TEXT, period_to TEXT, source_file TEXT
);
CREATE TABLE benefit (
    id INTEGER PRIMARY KEY, year INTEGER, year_from INTEGER, year_to INTEGER,
    region_code TEXT, tax_code TEXT, oktmo TEXT, mo_name TEXT, payer TEXT,
    category TEXT, kind TEXT, size_text TEXT, size_value REAL, size_unit TEXT,
    condition TEXT, basis TEXT, npa_name TEXT, npa_number TEXT, npa_date TEXT,
    npa_authority TEXT, period_from TEXT, period_to TEXT, source_file TEXT
);
"""


def test_old_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(OLD_SCHEMA)
    old.execute("INSERT INTO benefit (year, year_from, region_code, tax_code, payer, category)"
                " VALUES (2023, 2023, '77', 'tn', 'fl', 'Пенсионеры')")
    old.execute("INSERT INTO benefit (year, year_from, region_code, tax_code, payer, category)"
                " VALUES (2023, 2023, '77', 'tn', 'ul', 'Организации')")
    old.execute("INSERT INTO rate (year, year_from, region_code, tax_code, payer, object_name)"
                " VALUES (2023, 2023, '77', 'tn', 'fl', 'Автомобили легковые')")
    old.commit()
    old.close()

    # раньше здесь падало: CREATE INDEX по столбцу, которого нет в старой таблице
    db.init_db(path)

    conn = db.connect(path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(benefit)")}
    assert {"for_fl", "for_ul", "for_ip"} <= columns
    assert "object_group" in {row["name"] for row in conn.execute("PRAGMA table_info(rate)")}

    # прежние записи не теряют категорию: флаги заполняются из старого поля payer
    counts = analytics.benefit_counts(conn, "tn", 2023, "77")
    assert (counts["fl"], counts["ul"], counts["total"]) == (1, 1, 2)
    assert analytics.benefits(conn, "tn", 2023, "77", payer="fl")["total"] == 1
    assert analytics.rates(conn, "tn", 2023, "77", payer="fl")["total"] == 1
    conn.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "db.sqlite"
    db.init_db(path)
    db.init_db(path)
    conn = db.connect(path)
    assert db.migrate(conn) == []
    conn.close()
