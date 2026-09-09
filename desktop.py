"""Точка входа собранного приложения (exe).

Что происходит при запуске:
  1. рабочей папкой становится каталог, где лежит сам файл программы;
  2. при первом запуске рядом появляются `reference/` (справочники, их можно
     править) и `data/` (база);
  3. программа обходит свою папку, находит выгрузки ФНС и загружает новые;
  4. поднимается локальный сервер и открывается браузер.

Достаточно положить файлы ФНС рядом с программой и запустить её заново —
новые файлы подхватятся, уже загруженные повторно обрабатываться не будут.
"""
from __future__ import annotations

import argparse
import multiprocessing
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from fnsportal import analytics, autoload, config, db
from fnsportal.cli import _load_reference
from fnsportal.sources import methodology

BANNER = r"""
  ╔══════════════════════════════════════════════════════════╗
  ║   Аналитический налоговый портал ФНС                     ║
  ║   ставки · льготы · статотчётность · аналитика           ║
  ╚══════════════════════════════════════════════════════════╝
"""


def setup_console() -> None:
    """Немедленный вывод и устойчивость к кодировке консоли.

    В Windows при перенаправлении вывода в файл используется cp1251, где нет
    ни рамок, ни стрелок, — без errors="replace" запуск падал бы на первом же
    сообщении.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def prepare(reimport: bool = False, allow_demo: bool = True) -> tuple[int, int, int]:
    """Готовит базу и загружает всё новое из рабочей папки."""
    config.ensure_dirs()
    restored = config.ensure_reference()
    if restored:
        print(f"  распакованы справочники: {', '.join(restored)}")

    db.init_db(config.DB_PATH)
    conn = db.connect(config.DB_PATH)
    _load_reference(conn)
    map_csv = config.REFERENCE_DIR / "form_5tn_map.csv"
    if map_csv.exists():
        methodology.load_into_db(conn, methodology.read_map_csv(map_csv))

    if reimport:
        with conn:
            conn.execute("DELETE FROM imported_file")
        print("  история загрузок очищена — файлы будут прочитаны заново")

    print(f"\n  Рабочая папка: {config.ROOT}")
    print("  Ищу файлы данных…")
    autoload.run(conn, config.ROOT)

    counts = tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("fact_form", "rate", "benefit")
    )
    if allow_demo and not any(counts):
        print("\n  Данных нет. Положите выгрузки ФНС рядом с программой"
              " и запустите её снова.")
        answer = input("  Пока показать демонстрационный набор? [Enter — да, n — нет]: ")
        if answer.strip().lower() not in {"n", "н", "no", "нет"}:
            from fnsportal import demo

            print("  генерирую демонстрационные данные…")
            demo.generate(conn)
            analytics.build_terms(conn)
            counts = tuple(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("fact_form", "rate", "benefit")
            )
    conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Аналитический налоговый портал")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--reimport", action="store_true",
                        help="перечитать все файлы, даже уже загруженные")
    parser.add_argument("--no-demo", action="store_true",
                        help="не предлагать демонстрационные данные")
    args = parser.parse_args(argv)

    setup_console()
    print(BANNER)
    try:
        facts, rates_count, benefits = prepare(args.reimport, allow_demo=not args.no_demo)
    except Exception:  # noqa: BLE001 — окно не должно закрыться молча
        traceback.print_exc()
        input("\n  Произошла ошибка. Нажмите Enter, чтобы закрыть окно…")
        return 1

    print(f"\n  В базе: {facts} значений форм · {rates_count} ставок · {benefits} льгот")

    import uvicorn

    from fnsportal.api import create_app

    port = free_port(args.port)
    url = f"http://{args.host}:{port}"
    print(f"\n  Портал: {url}")
    print("  Закрыть — Ctrl+C или закройте это окно.\n")

    if not args.no_browser:
        def open_browser() -> None:
            time.sleep(1.5)
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 — браузер не критичен
                pass

        threading.Thread(target=open_browser, daemon=True).start()

    try:
        uvicorn.run(create_app(config.DB_PATH), host=args.host, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
