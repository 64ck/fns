"""Скачивание исходников с сайта ФНС.

Модуль намеренно не «зашивает» точные адреса файлов: ФНС меняет имена
выгрузок. Вместо этого он открывает страницу набора/раздела, находит на ней
ссылки на файлы данных и качает подходящие. Всегда можно посмотреть, что
именно будет скачано, флагом --dry-run.

Скачивание с докачкой (HTTP Range): выгрузка ставок и льгот в HTML весит
около 3 ГБ, и обрыв связи не должен означать «начать сначала».
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Sequence

from .. import config

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DATA_SUFFIXES = (".csv", ".zip", ".xls", ".xlsx", ".xml", ".rar", ".7z", ".html", ".htm")
LINK_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)

FORM_KEYWORDS = {
    "tn": ("5tn", "5-тн", "5_тн", "транспорт"),
    "zn": ("5mn", "5-мн", "земель"),
    "nifl": ("5mn", "5-мн", "имуществ"),
    "niul": ("5nio", "5-нио", "имуществ"),
}


def fetch(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def find_links(page_url: str, html: bytes, suffixes: Sequence[str] = DATA_SUFFIXES) -> list[str]:
    text = html.decode("utf-8", "replace")
    if "windows-1251" in text[:2000].lower() or "cp1251" in text[:2000].lower():
        text = html.decode("cp1251", "replace")
    links = []
    for href in LINK_RE.findall(text):
        absolute = urllib.parse.urljoin(page_url, href.strip())
        path = urllib.parse.urlparse(absolute).path.lower()
        if path.endswith(tuple(suffixes)):
            links.append(absolute)
    seen: set[str] = set()
    return [link for link in links if not (link in seen or seen.add(link))]


def download(url: str, destination: Path, *, retries: int = 4, chunk: int = 1 << 20) -> Path:
    """Качает файл с докачкой и повторами при обрыве связи."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, retries + 1):
        position = destination.stat().st_size if destination.exists() else 0
        headers = {"User-Agent": USER_AGENT}
        if position:
            headers["Range"] = f"bytes={position}-"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                mode = "ab" if position and response.status == 206 else "wb"
                if mode == "wb":
                    position = 0
                total = int(response.headers.get("Content-Length") or 0) + position
                done = position
                with destination.open(mode) as fh:
                    while True:
                        block = response.read(chunk)
                        if not block:
                            break
                        fh.write(block)
                        done += len(block)
                        if total:
                            print(f"\r   {destination.name}: {done/1e6:8.1f} / {total/1e6:.1f} МБ"
                                  f" ({done * 100 // total}%)", end="")
                        else:
                            print(f"\r   {destination.name}: {done/1e6:8.1f} МБ", end="")
            print()
            return destination
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            wait = 2 ** attempt
            print(f"\n   ! обрыв ({error}); повтор через {wait} с "
                  f"[{attempt}/{retries}]")
            time.sleep(wait)
    raise RuntimeError(f"не удалось скачать {url}")


def download_rates(out_dir: Path, url: str | None = None, dry_run: bool = False) -> int:
    """Набор «Ставки и льготы по имущественным налогам» (opendata ФНС)."""
    page_url = url or config.OPENDATA_RATES_PAGE
    print(f"Страница набора: {page_url}")
    try:
        html = fetch(page_url)
    except Exception as error:  # noqa: BLE001 — сообщение важнее типа
        print(f"Не удалось открыть страницу: {error}")
        print("Скачайте файл вручную и загрузите: python -m fnsportal load-rates <файл>")
        return 1
    links = find_links(page_url, html)
    if not links:
        print("На странице не найдено ссылок на файлы данных.")
        print("Проверьте адрес набора или скачайте файл вручную.")
        return 1
    print(f"Найдено файлов: {len(links)}")
    for link in links:
        print(f"  {link}")
    if dry_run:
        return 0
    for link in links:
        name = Path(urllib.parse.urlparse(link).path).name
        download(link, out_dir / name)
    print(f"Готово. Файлы в {out_dir}")
    print(f"Дальше: python -m fnsportal load-rates {out_dir}/*")
    return 0


def download_forms(
    out_dir: Path,
    regions: Iterable[str] | None = None,
    years: Iterable[int] | None = None,
    tax: str = "tn",
    dry_run: bool = False,
) -> int:
    """Формы статотчётности по регионам.

    Страница раздела своя у каждого УФНС (в адресе — код региона: rn77, rn50…),
    поэтому обход идёт по списку регионов.
    """
    codes = list(regions or [])
    if not codes:
        print("Укажите регионы: --regions 77 50 16 (или ALL для всех кодов справочника)")
        return 1
    if codes == ["ALL"]:
        import csv as _csv

        with (config.REFERENCE_DIR / "regions.csv").open(encoding="utf-8") as fh:
            codes = [row["code"] for row in _csv.DictReader(fh)
                     if row["kind"] == "subject" and not row["valid_to"]]
    keywords = FORM_KEYWORDS.get(tax, ())
    wanted_years = {str(y) for y in (years or [])}
    total = 0
    for code in codes:
        page_url = config.FORMS_PAGE_TEMPLATE.format(region=code)
        try:
            html = fetch(page_url)
        except Exception as error:  # noqa: BLE001
            print(f"  {code}: страница недоступна ({error})")
            continue
        links = [
            link for link in find_links(page_url, html)
            if any(word in link.lower() for word in keywords)
            and (not wanted_years or any(year in link for year in wanted_years))
        ]
        print(f"  {code}: найдено {len(links)}")
        for link in links:
            print(f"     {link}")
            if dry_run:
                continue
            name = Path(urllib.parse.urlparse(link).path).name
            download(link, out_dir / tax / code / name)
            total += 1
    if not dry_run:
        print(f"Скачано файлов: {total}. Дальше: "
              f"python -m fnsportal load-forms {out_dir}/{tax} --tax {tax}")
    return 0
