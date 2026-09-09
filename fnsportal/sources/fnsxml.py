"""Потоковое чтение XML-выгрузки ФНС «Ставки и льготы по имущественным налогам».

Открытый набор 7707329152-taxrates выгружается одним XML-файлом на несколько
гигабайт со структурой:

    <List>
      <li ID="2601" List_value="77 - город Москва"/>
      <li ID="2802" List_value="Транспортный налог"/>
    </List>
    ...
    <tp ID="…" Region_ID="2601" Nalog_ID="2802" TaxPeriod="2023" MunObraz="…"
        Oktmo_ID="…" LawDoc="…" LawNum="…" LawDate="…">
      <tr TaxObject="Автомобили легковые…" TaxRates="12" Fl="1" UL="1" IP="0"/>
      <tb Category="Пенсионеры…" Amount="100" Unit="%" Condition="…" Base="…"
          LawArticle="…" Fl="1" UL="0" IP="0"/>
    </tp>

Здесь `<tr>` — ставки, `<tb>` — льготы, а категория плательщика задана явно
атрибутами Fl / UL / IP, поэтому её не нужно угадывать по тексту.

Файл читается кусками: в памяти живёт одна запись, а не документ. Атрибуты
разбираются регулярными выражениями, а не полноценным XML-парсером, потому что
в разделе List выгрузки встречаются значения, не проходящие строгую проверку, —
на них штатный парсер останавливается и теряет весь остаток файла.
"""
from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

CHUNK = 4 << 20                      # 4 МБ
MAX_RECORD = 8 << 20                 # предохранитель от «записи» на пол-файла
LIST_LIMIT = 64 << 20                # раздел List идёт в начале и невелик

RECORD_TAG = "tp"                    # налоговый документ (TaxPlace)
RATE_TAG = "tr"                      # ставка
BENEFIT_TAG = "tb"                   # льгота

_ATTRIBUTE = re.compile(r"""([:\w.-]+)\s*=\s*(["'])(.*?)\2""", re.DOTALL)
_RECORD_START = re.compile(rf"<{RECORD_TAG}\b", re.I)
_RECORD_END = re.compile(rf"</{RECORD_TAG}\s*>", re.I)
_CHILD = re.compile(rf"<({RATE_TAG}|{BENEFIT_TAG})\b([^>]*?)/?>", re.I | re.DOTALL)
_LIST_ITEM = re.compile(r"<li\b([^>]*?)/?>", re.I | re.DOTALL)
_LIST_END = re.compile(r"</List\s*>", re.I)
_ENCODING = re.compile(rb"""encoding\s*=\s*["']([\w-]+)["']""", re.I)

ProgressFn = Callable[[int], None]


@dataclass
class Record:
    """Один налоговый документ: реквизиты, ставки и льготы."""

    attrs: dict[str, str] = field(default_factory=dict)
    rates: list[dict[str, str]] = field(default_factory=list)
    benefits: list[dict[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.attrs)


def detect_encoding(head: bytes) -> str:
    match = _ENCODING.search(head[:4096])
    if match:
        name = match.group(1).decode("ascii", "ignore").lower()
        if name in {"windows-1251", "cp1251", "win-1251"}:
            return "cp1251"
        if name.startswith("utf-8"):
            return "utf-8"
        return name
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1251"


def looks_like_export(path: Path, probe: int = 256 * 1024) -> bool:
    """Быстрая проверка: это выгрузка ставок и льгот ФНС?"""
    try:
        with Path(path).open("rb") as fh:
            head = fh.read(probe)
    except OSError:
        return False
    encoding = detect_encoding(head)
    text = head.decode(encoding, "replace")
    return bool(_RECORD_START.search(text)) or (
        "List_value" in text and "<li " in text.lower())


def parse_attributes(raw: str) -> dict[str, str]:
    return {
        match.group(1): html_mod.unescape(match.group(3))
        for match in _ATTRIBUTE.finditer(raw)
    }


def read_list_values(path: Path, limit: int = LIST_LIMIT) -> dict[str, str]:
    """Справочник «ID -> значение» из раздела List в начале файла.

    Через него раскрываются Region_ID («77 - город Москва») и Nalog_ID
    («Транспортный налог»).
    """
    path = Path(path)
    with path.open("rb") as fh:
        head = fh.read(min(limit, path.stat().st_size))
    text = head.decode(detect_encoding(head), "replace")
    end = _LIST_END.search(text)
    section = text[: end.start()] if end else text
    values: dict[str, str] = {}
    for match in _LIST_ITEM.finditer(section):
        attrs = parse_attributes(match.group(1))
        ident, value = attrs.get("ID"), attrs.get("List_value")
        if ident and value:
            values[ident] = value
    return values


def iter_records(path: Path, on_progress: ProgressFn | None = None) -> Iterator[Record]:
    """Потоково выдаёт документы выгрузки; память не зависит от размера файла."""
    path = Path(path)
    with path.open("rb") as fh:
        head = fh.read(64 * 1024)
    encoding = detect_encoding(head)

    import codecs

    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    buffer = ""
    read_bytes = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            read_bytes += len(chunk)
            if on_progress:
                on_progress(read_bytes)
            buffer += decoder.decode(chunk)
            position = 0
            while True:
                start = _RECORD_START.search(buffer, position)
                if not start:
                    break
                head_end = buffer.find(">", start.end())
                if head_end == -1:
                    break
                if buffer[head_end - 1] == "/":            # <tp …/> без вложений
                    record = Record(parse_attributes(buffer[start.end(): head_end - 1]))
                    position = head_end + 1
                    yield record
                    continue
                end = _RECORD_END.search(buffer, head_end)
                if not end:
                    break
                yield _build(buffer[start.end(): head_end], buffer[head_end + 1: end.start()])
                position = end.end()
            if position:
                buffer = buffer[position:]
            start = _RECORD_START.search(buffer)
            if start is None:
                buffer = buffer[-16:]
            elif start.start():
                buffer = buffer[start.start():]
            if len(buffer) > MAX_RECORD:                   # запись без закрывающего тега
                buffer = buffer[-CHUNK:]
        buffer += decoder.decode(b"", final=True)
    start = _RECORD_START.search(buffer)
    if start:
        head_end = buffer.find(">", start.end())
        if head_end != -1:
            end = _RECORD_END.search(buffer, head_end)
            inner = buffer[head_end + 1: end.start()] if end else buffer[head_end + 1:]
            yield _build(buffer[start.end(): head_end], inner)


def _build(head: str, inner: str) -> Record:
    record = Record(parse_attributes(head.rstrip("/")))
    for match in _CHILD.finditer(inner):
        attrs = parse_attributes(match.group(2))
        if match.group(1).lower() == RATE_TAG:
            record.rates.append(attrs)
        else:
            record.benefits.append(attrs)
    return record


def payer_flags(attrs: dict[str, str]) -> dict[str, bool]:
    """Категории плательщиков, к которым относится ставка или льгота."""
    def flag(*names: str) -> bool:
        for name in names:
            value = attrs.get(name)
            if value is not None:
                return str(value).strip() in {"1", "true", "True", "да", "Да"}
        return False

    return {
        "fl": flag("Fl", "FL", "fl"),
        "ul": flag("UL", "Ul", "ul", "Yur"),
        "ip": flag("IP", "Ip", "ip"),
    }


def object_group(text: str) -> tuple[str, str]:
    """Делит объект налогообложения на группу и уточнение.

    «Гидроциклы … (с каждой лошадиной силы): свыше 100 л.с.» -> («Гидроциклы …»,
    «свыше 100 л.с.»), чтобы объекты можно было группировать, а не показывать
    в том порядке, в каком их набрал регион.
    """
    text = " ".join(str(text or "").split())
    head, separator, tail = text.partition(":")
    if separator and head.strip():
        return head.strip(), tail.strip()
    return text, ""
