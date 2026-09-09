"""Командная строка портала: подготовка БД, загрузка данных, запуск сервера."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

from . import analytics, config, db
from .sources import forms, methodology, rates, tablestream


def _load_reference(conn: sqlite3.Connection) -> dict:
    """Заливает справочники регионов и налогов из reference/*.csv."""
    counts = {}
    regions_path = config.REFERENCE_DIR / "regions.csv"
    with regions_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    with conn:
        conn.executemany(
            """INSERT INTO region (code, name, short_name, federal_district, kind,
                                   valid_from, valid_to, note)
               VALUES (:code, :name, :short_name, :federal_district, :kind,
                       NULLIF(:valid_from,''), NULLIF(:valid_to,''), :note)
               ON CONFLICT (code) DO UPDATE SET
                   name=excluded.name, short_name=excluded.short_name,
                   federal_district=excluded.federal_district, kind=excluded.kind,
                   note=excluded.note""", rows)
    counts["regions"] = len(rows)

    taxes_path = config.REFERENCE_DIR / "taxes.csv"
    with taxes_path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    with conn:
        conn.executemany(
            """INSERT INTO tax (code, name, short_name, level, form_code, sort_order)
               VALUES (:code, :name, :short_name, :level, :form_code, :sort_order)
               ON CONFLICT (code) DO UPDATE SET
                   name=excluded.name, short_name=excluded.short_name,
                   level=excluded.level, form_code=excluded.form_code""", rows)
    counts["taxes"] = len(rows)
    return counts


# ------------------------------------------------------------------- команды

def cmd_init(args: argparse.Namespace) -> int:
    config.ensure_dirs()
    path = db.init_db(args.db)
    conn = db.connect(args.db)
    counts = _load_reference(conn)
    map_csv = config.REFERENCE_DIR / "form_5tn_map.csv"
    if map_csv.exists():
        meth = methodology.read_map_csv(map_csv)
        counts["indicators"], counts["indicator_codes"] = methodology.load_into_db(conn, meth)
    print(f"База: {path}")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    return 0


def cmd_load_methodology(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    src = Path(args.path)
    if src.suffix.lower() in {".xlsx", ".xlsm"}:
        meth = methodology.parse_workbook(src, default_tax=args.tax)
        out = methodology.write_map_csv(meth, config.REFERENCE_DIR / f"form_5{args.tax}_map.csv")
        print(f"Карта кодов строк сохранена: {out}")
    else:
        meth = methodology.read_map_csv(src)
    indicators, codes = methodology.load_into_db(conn, meth)
    print(f"Показателей: {indicators}, кодов строк: {codes}, годов: {len(meth.years)}")
    return 0


def cmd_load_rates(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    total = {"rows": 0, "rates": 0, "benefits": 0}
    for pattern in args.paths:
        for path in _expand(pattern):
            print(f"→ {path}")

            def progress(stats, path=path):
                print(f"   {stats.rows_read:>10} строк | ставок {stats.rates} | льгот {stats.benefits}",
                      end="\r", file=sys.stderr)

            stats = rates.load_file(
                conn, path, tax_code=args.tax, default_year=args.year,
                limit=args.limit, progress=progress)
            total["rows"] += stats.rows_read
            total["rates"] += stats.rates
            total["benefits"] += stats.benefits
            print(f"   строк: {stats.rows_read}, ставок: {stats.rates}, льгот: {stats.benefits}")
            if stats.unmapped_regions:
                print(f"   не опознаны регионы: {sorted(stats.unmapped_regions)[:5]}")
            if stats.unmapped_taxes:
                print(f"   не опознаны налоги: {sorted(stats.unmapped_taxes)[:5]}")
    print(f"Итого: {total}")
    return 0


def cmd_load_forms(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    loaded = failed = 0
    for pattern in args.paths:
        for path in _expand(pattern):
            if path.is_dir():
                results = forms.load_directory(conn, path, tax_code=args.tax, year=args.year)
            else:
                results = [forms.load_file(conn, path, tax_code=args.tax,
                                           year=args.year, region_code=args.region)]
            for stats in results:
                if stats.problems:
                    failed += 1
                    print(f"   ! {stats.file}: {'; '.join(stats.problems)}")
                else:
                    loaded += 1
                    print(f"   ✓ {stats.file}: {stats.region_code} / {stats.year} — "
                          f"{stats.values} значений")
    inserted = forms.recompute_totals(conn, args.tax)
    print(f"Загружено файлов: {loaded}, с ошибками: {failed}, строк 'всего': {inserted}")
    return 0


def cmd_build_terms(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    result = analytics.build_terms(
        conn, tax_code=args.tax, top_per_bucket=args.top,
        fields=tuple(f.strip() for f in args.fields.split(",") if f.strip()),
        bigrams=not args.no_bigrams,
        extra_stopwords=tuple(w.strip() for w in (args.stopwords or "").split(",") if w.strip()),
        progress=lambda n: print(f"   обработано льгот: {n}", end="\r", file=sys.stderr))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    for pattern in args.paths:
        for path in _expand(pattern):
            print(f"\n=== {path} ({path.stat().st_size/1e6:.1f} МБ)")
            for table in tablestream.describe(path, max_rows=args.rows):
                print(f"  таблица: {table['table']}  строк(проба): {table['sampled_rows']}"
                      f"  колонок: {table['max_width']}  заголовок в строке: {table['header_row']}")
                if table["header"]:
                    aliases = rates.load_aliases()
                    mapping = rates.map_columns(table["header"], aliases)
                    print("  распознанные колонки:")
                    for field, column in mapping.items():
                        print(f"    {field:18} <- {column}")
                    missing = [c for c in table["header"] if c and c not in mapping.values()]
                    if missing:
                        print(f"  не распознаны: {missing[:12]}")
                for row in table["preview"][: args.rows]:
                    print("   |", " | ".join(str(c)[:28] for c in row[:10]))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from . import demo

    config.ensure_dirs()
    db.init_db(args.db)
    conn = db.connect(args.db)
    _load_reference(conn)
    map_csv = config.REFERENCE_DIR / "form_5tn_map.csv"
    if map_csv.exists():
        methodology.load_into_db(conn, methodology.read_map_csv(map_csv))
    result = demo.generate(conn, year_from=args.year_from, year_to=args.year_to, seed=args.seed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    queries = {
        "регионов": "SELECT COUNT(*) FROM region",
        "налогов": "SELECT COUNT(*) FROM tax",
        "показателей": "SELECT COUNT(*) FROM indicator",
        "кодов строк": "SELECT COUNT(*) FROM indicator_code",
        "значений форм": "SELECT COUNT(*) FROM fact_form",
        "ставок": "SELECT COUNT(*) FROM rate",
        "льгот": "SELECT COUNT(*) FROM benefit",
        "терминов": "SELECT COUNT(*) FROM benefit_term",
    }
    for label, sql in queries.items():
        print(f"  {label:>16}: {conn.execute(sql).fetchone()[0]:,}".replace(",", " "))
    print("\n  данные по налогам и годам:")
    for row in conn.execute(
        """SELECT tax_code, MIN(year) AS y1, MAX(year) AS y2,
                  COUNT(DISTINCT region_code) AS regions, COUNT(*) AS n
             FROM fact_form GROUP BY tax_code"""):
        print(f"    {row['tax_code']}: {row['y1']}–{row['y2']}, регионов {row['regions']}, значений {row['n']}")
    for row in conn.execute(
        """SELECT tax_code, COUNT(*) AS n, COUNT(DISTINCT region_code) AS regions
             FROM benefit GROUP BY tax_code"""):
        print(f"    льготы {row['tax_code']}: {row['n']} записей, регионов {row['regions']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    app = create_app(args.db)
    print(f"Портал: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    from .sources import download

    if args.what == "rates":
        return download.download_rates(Path(args.out or config.RAW_RATES_DIR),
                                       url=args.url, dry_run=args.dry_run)
    return download.download_forms(
        Path(args.out or config.RAW_FORMS_DIR), regions=args.regions,
        years=args.years, tax=args.tax, dry_run=args.dry_run)


def _expand(pattern: str) -> list[Path]:
    path = Path(pattern)
    if path.exists():
        return [path]
    matches = sorted(Path().glob(pattern))
    if not matches:
        print(f"   ! ничего не найдено: {pattern}")
    return matches


# --------------------------------------------------------------------- парсер

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fnsportal",
        description="Аналитический налоговый портал ФНС: ETL и локальный сервер",
    )
    parser.add_argument("--db", default=str(config.DB_PATH), help="путь к файлу SQLite")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="создать БД и загрузить справочники")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("load-methodology", help="загрузить методичку по кодам строк формы")
    p.add_argument("path", nargs="?", default=str(config.REFERENCE_DIR / "form_5tn_methodology.xlsx"))
    p.add_argument("--tax", default="tn")
    p.set_defaults(func=cmd_load_methodology)

    p = sub.add_parser("load-rates", help="загрузить ставки и льготы (opendata taxrates)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--tax", default=None, help="принудительно задать налог (tn/zn/nifl/niul)")
    p.add_argument("--year", type=int, default=None, help="год по умолчанию")
    p.add_argument("--limit", type=int, default=None, help="ограничить число строк (для проб)")
    p.set_defaults(func=cmd_load_rates)

    p = sub.add_parser("load-forms", help="загрузить формы статотчётности")
    p.add_argument("paths", nargs="+")
    p.add_argument("--tax", default="tn")
    p.add_argument("--year", type=int, default=None)
    p.add_argument("--region", default=None)
    p.set_defaults(func=cmd_load_forms)

    p = sub.add_parser("build-terms", help="построить частотный словарь для облаков слов")
    p.add_argument("--tax", default=None)
    p.add_argument("--top", type=int, default=400, help="сколько терминов хранить на ведро")
    p.add_argument("--fields", default="category",
                   help="какие тексты льгот индексировать: category,kind,condition")
    p.add_argument("--no-bigrams", action="store_true", help="только отдельные слова")
    p.add_argument("--stopwords", default=None,
                   help="дополнительные стоп-слова через запятую")
    p.set_defaults(func=cmd_build_terms)

    p = sub.add_parser("inspect", help="показать структуру файла-источника")
    p.add_argument("paths", nargs="+")
    p.add_argument("--rows", type=int, default=6)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("demo", help="сгенерировать демонстрационный набор данных")
    p.add_argument("--year-from", type=int, default=2015)
    p.add_argument("--year-to", type=int, default=2024)
    p.add_argument("--seed", type=int, default=20240101)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("stats", help="что уже загружено в БД")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("serve", help="запустить локальный портал")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("download", help="скачать исходники с сайта ФНС")
    p.add_argument("what", choices=["rates", "forms"])
    p.add_argument("--out", default=None)
    p.add_argument("--url", default=None)
    p.add_argument("--regions", nargs="*", default=None)
    p.add_argument("--years", nargs="*", type=int, default=None)
    p.add_argument("--tax", default="tn")
    p.add_argument("--dry-run", action="store_true", help="только показать найденные ссылки")
    p.set_defaults(func=cmd_download)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
