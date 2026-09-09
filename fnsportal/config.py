"""Пути и настройки проекта.

Работает в двух режимах:

* обычный запуск из исходников — корень проекта берётся от файла модуля;
* собранный exe (PyInstaller) — рабочей папкой становится каталог, где лежит
  сам exe. Туда же пользователь кладёт выгрузки ФНС, там же создаётся база и
  распаковываются редактируемые справочники.

Любой путь можно переопределить переменной окружения.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

# Каталог с ресурсами, вшитыми в сборку (статика портала, эталонные справочники)
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))

# Рабочая папка: рядом с exe либо корень репозитория
_DEFAULT_ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("FNS_ROOT", _DEFAULT_ROOT))

DATA_DIR = Path(os.environ.get("FNS_DATA_DIR", ROOT / "data"))
RAW_DIR = Path(os.environ.get("FNS_RAW_DIR", DATA_DIR / "raw"))
DB_PATH = Path(os.environ.get("FNS_DB", DATA_DIR / "db" / "fns.sqlite"))
REFERENCE_DIR = Path(os.environ.get("FNS_REFERENCE_DIR", ROOT / "reference"))
STATIC_DIR = Path(os.environ.get("FNS_STATIC_DIR", BUNDLE_DIR / "portal" / "static"))

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

# Файлы справочников, которые должны лежать рядом с exe и быть доступны для правки
REFERENCE_FILES = (
    "regions.csv",
    "taxes.csv",
    "tax_aliases.csv",
    "column_aliases.json",
    "stopwords_ru.txt",
    "stopwords_legal.txt",
    "form_5tn_map.csv",
    "form_5tn_methodology.xlsx",
)


def ensure_dirs() -> None:
    for path in (DATA_DIR, RAW_DIR, RAW_RATES_DIR, RAW_FORMS_DIR, DB_PATH.parent):
        path.mkdir(parents=True, exist_ok=True)


def ensure_reference() -> list[str]:
    """Распаковывает справочники из сборки рядом с exe (только недостающие).

    Пользователь может править стоп-слова и алиасы колонок, поэтому файлы
    живут снаружи и при обновлении программы не перезаписываются.
    """
    if REFERENCE_DIR == BUNDLE_DIR / "reference":
        return []
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    restored = []
    for name in REFERENCE_FILES:
        target = REFERENCE_DIR / name
        source = BUNDLE_DIR / "reference" / name
        if not target.exists() and source.exists():
            shutil.copyfile(source, target)
            restored.append(name)
    return restored
