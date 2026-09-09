"""HTTP-API портала и раздача статики.

Запуск: python -m fnsportal serve  (или uvicorn fnsportal.api:app)
"""
from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import analytics, config, db
from .textutil import LEMMATIZER


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def create_app(db_path: str | Path | None = None) -> FastAPI:
    database = Path(db_path or config.DB_PATH)
    app = FastAPI(title="Аналитический налоговый портал", version="0.1.0")
    connection = db.connect(database, readonly=True) if database.exists() else db.connect(database)

    def conn() -> sqlite3.Connection:
        return connection

    # ------------------------------------------------------------ справочники
    @app.get("/api/meta")
    def meta(c: sqlite3.Connection = Depends(conn)) -> dict:
        counts = {
            key: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for key, table in (
                ("regions", "region"), ("indicators", "indicator"),
                ("facts", "fact_form"), ("rates", "rate"),
                ("benefits", "benefit"), ("terms", "benefit_term"),
            )
        }
        loads = [dict(r) for r in c.execute(
            "SELECT loaded_at, kind, source_file, rows_read, rows_saved"
            " FROM load_log ORDER BY id DESC LIMIT 10")]
        return {
            "taxes": analytics.taxes(c),
            "payers": config.PAYER_NAMES,
            "years": analytics.years(c),
            "counts": counts,
            "lemmatizer": LEMMATIZER,
            "recent_loads": loads,
            "db_path": str(database),
        }

    @app.get("/api/regions")
    def regions(tax: str | None = None, year: int | None = None,
                c: sqlite3.Connection = Depends(conn)) -> list[dict]:
        return analytics.regions(c, tax, year)

    @app.get("/api/years")
    def years(tax: str | None = None, c: sqlite3.Connection = Depends(conn)) -> list[int]:
        return analytics.years(c, tax)

    @app.get("/api/indicators")
    def indicators(tax: str, year: int | None = None,
                   c: sqlite3.Connection = Depends(conn)) -> list[dict]:
        return analytics.indicators(c, tax, year)

    # --------------------------------------------------------------- профиль
    @app.get("/api/profile")
    def profile(tax: str, year: int, region: str,
                c: sqlite3.Connection = Depends(conn)) -> dict:
        return analytics.profile(c, tax, year, region)

    @app.get("/api/form")
    def form(tax: str, year: int, region: str,
             c: sqlite3.Connection = Depends(conn)) -> list[dict]:
        return analytics.form_table(c, tax, year, region)

    @app.get("/api/series")
    def series(tax: str, region: str, indicators: str,
               payer: str = "total", year_from: int | None = None,
               year_to: int | None = None, compare: bool = True,
               c: sqlite3.Connection = Depends(conn)) -> dict:
        ids = [int(i) for i in _split(indicators)]
        if not ids:
            raise HTTPException(400, "не выбраны показатели")
        return analytics.series(c, tax, region, ids, payer, year_from, year_to, compare)

    @app.get("/api/ranking")
    def ranking(tax: str, year: int, indicator: int, payer: str = "total",
                limit: int = 100, c: sqlite3.Connection = Depends(conn)) -> list[dict]:
        return analytics.ranking(c, tax, year, indicator, payer, limit)

    # ------------------------------------------------------- ставки и льготы
    @app.get("/api/rates")
    def rates(tax: str, year: int, region: str, payer: str | None = None,
              search: str = "", limit: int = 500, offset: int = 0,
              c: sqlite3.Connection = Depends(conn)) -> dict:
        return analytics.rates(c, tax, year, region,
                               None if payer in (None, "", "all_categories") else payer,
                               search, limit, offset)

    @app.get("/api/benefits")
    def benefits(tax: str, year: int, region: str, payer: str | None = None,
                 search: str = "", limit: int = 500, offset: int = 0,
                 c: sqlite3.Connection = Depends(conn)) -> dict:
        return analytics.benefits(c, tax, year, region,
                                  None if payer in (None, "", "all_categories") else payer,
                                  search, limit, offset)

    @app.get("/api/wordcloud")
    def wordcloud(tax: str, payer: str, years: str = "", regions: str = "",
                  limit: int = 120, mode: str = "freq", bigrams: bool = True,
                  c: sqlite3.Connection = Depends(conn)) -> dict:
        return analytics.wordcloud(
            c, tax, payer,
            [int(y) for y in _split(years)], _split(regions),
            limit=limit, mode=mode, include_bigrams=bigrams)

    # ---------------------------------------------------------------- экспорт
    @app.get("/api/export/{kind}.csv")
    def export(kind: str, tax: str, year: int | None = None, region: str | None = None,
               payer: str | None = None, indicators: str = "", search: str = "",
               c: sqlite3.Connection = Depends(conn)) -> StreamingResponse:
        rows: list[dict]
        if kind == "form" and year and region:
            rows = _flatten_form(analytics.form_table(c, tax, year, region))
        elif kind == "rates" and year and region:
            rows = analytics.rates(c, tax, year, region, payer, search, limit=100000)["items"]
        elif kind == "benefits" and year and region:
            rows = analytics.benefits(c, tax, year, region, payer, search, limit=100000)["items"]
        elif kind == "series" and region:
            data = analytics.series(c, tax, region, [int(i) for i in _split(indicators)],
                                    payer or "total")
            rows = [
                {"indicator": item["name"], "unit": item["unit"], **point}
                for item in data["series"].values() for point in item["points"]
            ]
        elif kind == "wordcloud":
            data = analytics.wordcloud(c, tax, payer or "fl",
                                       [int(y) for y in _split(indicators)],
                                       _split(region or ""), limit=1000)
            rows = data["items"]
        else:
            raise HTTPException(400, "неизвестный тип выгрузки или не хватает параметров")
        return _csv_response(rows, f"{kind}_{tax}_{year or ''}_{region or ''}.csv")

    # ----------------------------------------------------------------- статика
    static_dir = config.STATIC_DIR
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(static_dir / "index.html")

    return app


def _flatten_form(rows: Iterable[dict]) -> list[dict]:
    out = []
    for row in rows:
        flat = {
            "section": row["section"], "indicator": row["name"], "unit": row["unit"],
        }
        for payer in ("ul", "fl", "total"):
            cell = row.get(payer) or {}
            flat[f"{payer}_значение"] = cell.get("value")
            flat[f"{payer}_среднее_по_субъектам"] = cell.get("avg")
            flat[f"{payer}_ранг"] = cell.get("rank")
        out.append(flat)
    return out


def _csv_response(rows: Sequence[dict[str, Any]], filename: str) -> StreamingResponse:
    buffer = io.StringIO()
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    data = "﻿" + buffer.getvalue()      # BOM, чтобы Excel открыл в UTF-8
    return StreamingResponse(
        iter([data]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


app = create_app()
