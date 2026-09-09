"""Пути и настройки проекта.

Все пути можно переопределить переменными окружения, чтобы держать тяжёлые
исходники ФНС (HTML-выгрузка ставок и льгот ~3 ГБ) вне репозитория.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("FNS_ROOT", Path(__file__).resolve().parent.parent))

DATA_DIR = Path(os.environ.get("FNS_DATA_DIR", ROOT / "data"))
RAW_DIR = Path(os.environ.get("FNS_RAW_DIR", DATA_DIR / "raw"))
DB_PATH = Path(os.environ.get("FNS_DB", DATA_DIR / "db" / "fns.sqlite"))
REFERENCE_DIR = Path(os.environ.get("FNS_REFERENCE_DIR", ROOT / "reference"))
STATIC_DIR = Path(os.environ.get("FNS_STATIC_DIR", ROOT / "portal" / "static"))

# Каталоги для исходников по видам данных (используются командами download/load-all)
RAW_RATES_DIR = RAW_DIR / "rates"
RAW_FORMS_DIR = RAW_DIR / "forms"

# Источники данных ФНС
OPENDATA_RATES_PAGE = "https://www.nalog.gov.ru/opendata/7707329152-taxrates/"
FORMS_PAGE_TEMPLATE = (
    "https://www.nalog.gov.ru/rn{region}/related_activities/"
    "statistics_and_analytics/forms/"
)

# Категории плательщиков
PAYERS = ("fl", "ip", "ul")
PAYER_NAMES = {
    "fl": "Физические лица",
    "ip": "Индивидуальные предприниматели",
    "ul": "Юридические лица",
    "all": "Все категории",
}

# Код агрегата «Российская Федерация» в справочнике регионов
RF_CODE = "00"


def ensure_dirs() -> None:
    for path in (DATA_DIR, RAW_DIR, RAW_RATES_DIR, RAW_FORMS_DIR, DB_PATH.parent):
        path.mkdir(parents=True, exist_ok=True)
