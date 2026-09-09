import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fnsportal import config, db  # noqa: E402
from fnsportal.sources import methodology  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    """Пустая БД со справочниками и картой показателей 5-ТН."""
    path = tmp_path / "test.sqlite"
    db.init_db(path)
    connection = db.connect(path)
    with (config.REFERENCE_DIR / "regions.csv").open(encoding="utf-8") as fh:
        connection.executemany(
            "INSERT INTO region(code,name,short_name,federal_district,kind,valid_from,"
            "valid_to,note) VALUES(:code,:name,:short_name,:federal_district,:kind,"
            "NULLIF(:valid_from,''),NULLIF(:valid_to,''),:note)", list(csv.DictReader(fh)))
    with (config.REFERENCE_DIR / "taxes.csv").open(encoding="utf-8") as fh:
        connection.executemany(
            "INSERT INTO tax(code,name,short_name,level,form_code,sort_order)"
            " VALUES(:code,:name,:short_name,:level,:form_code,:sort_order)",
            list(csv.DictReader(fh)))
    connection.commit()
    meth = methodology.read_map_csv(config.REFERENCE_DIR / "form_5tn_map.csv")
    methodology.load_into_db(connection, meth)
    yield connection
    connection.close()
