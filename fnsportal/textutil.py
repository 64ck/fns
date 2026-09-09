"""Нормализация текста, русский стеммер и стоп-слова.

Используется для:
  * сопоставления заголовков колонок и названий показателей;
  * определения категории плательщика (ФЛ / ИП / ЮЛ) по тексту льготы;
  * построения облаков слов по текстам льгот.

Лемматизация: если в окружении установлен pymorphy3 — используется он,
иначе применяется встроенный стеммер Портера для русского языка (Snowball).
Внешних зависимостей это не требует.
"""
from __future__ import annotations

import functools
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Iterator

from . import config

# --------------------------------------------------------------------- общее

_WS_RE = re.compile(r"\s+")
# Неразрывные и «типографские» пробелы, которыми ФНС разделяет разряды чисел
_SPECIAL = {"\u00a0": " ", "\u202f": " ", "\u2007": " ", "\u2009": " ", "–": "-"}
_TRANSLATION = str.maketrans(_SPECIAL)
_SPECIAL_CHARS = tuple(_SPECIAL)


def clean(text: object) -> str:
    """Схлопывает пробелы и чинит неразрывные пробелы.

    Вызывается по нескольку раз на каждую ячейку, а ячеек в выгрузке ФНС
    десятки миллионов, поэтому обычный случай (короткая строка без спецпробелов
    и без двойных пробелов) проходит без единой регулярки.
    """
    if text is None:
        return ""
    value = text if type(text) is str else str(text)
    if not value:
        return ""
    if any(char in value for char in _SPECIAL_CHARS):
        value = value.translate(_TRANSLATION)
    value = value.strip()
    if "  " in value or "\n" in value or "\t" in value or "\r" in value:
        value = _WS_RE.sub(" ", value)
    return value


def norm_key(text: object) -> str:
    """Ключ для сопоставления названий: нижний регистр, только буквы и цифры."""
    s = clean(text).lower().replace("ё", "е")
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[^0-9a-zа-я]+", " ", s)
    return _WS_RE.sub(" ", s).strip()


_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_number(value: object) -> float | None:
    """Достаёт число из ячейки ФНС: '1 234,5', '2,2 %', '-', 'x'."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = clean(value)
    if not s or s in {"-", "—", "х", "x", "*", "нет", "н/д"}:
        return None
    s = s.replace(" ", "").replace(" ", "")
    m = _NUM_RE.search(s.replace(",", "."))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


_YEAR_RE = re.compile(r"(19|20)\d{2}")


def parse_year(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and 1990 <= value <= 2100:
        return value
    m = _YEAR_RE.search(clean(value))
    return int(m.group(0)) if m else None


# ------------------------------------------------- категория плательщика льготы

_PAYER_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ip", (
        "индивидуальный предприниматель", "индивидуальные предприниматели",
        "предприниматель без образования юридического лица", "пбоюл",
        "глава крестьянского фермерского хозяйства", "самозанятый",
    )),
    ("ul", (
        "юридическое лицо", "организация", "предприятие", "учреждение",
        "общество", "товарищество", "кооператив", "юр лицо", "юл",
    )),
    ("fl", (
        "физическое лицо", "гражданин", "пенсионер", "ветеран", "инвалид",
        "многодетный", "семья", "родитель", "фл", "население",
    )),
)


def stem_phrase(text: object) -> str:
    """Приводит фразу к последовательности основ слов: «физических лиц» -> «физическ лиц»."""
    return " ".join(stem(token) for token in tokenize(clean(text)))


@functools.lru_cache(maxsize=1)
def _payer_stems() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((payer, tuple(stem_phrase(p) for p in phrases)) for payer, phrases in _PAYER_PATTERNS)


@functools.lru_cache(maxsize=100_000)
def _detect_payer_cached(text: str) -> str:
    return _detect_payer_impl(text)


def detect_payer_single(text: object) -> str:
    """Категория плательщика по одной формулировке (с кэшем повторов)."""
    if text is None:
        return "all"
    key = str(text)
    return _detect_payer_cached(key) if len(key) <= 500 else _detect_payer_impl(key)


def _detect_payer_impl(text: object) -> str:
    """Разбор формулировки без кэша: 'fl' | 'ip' | 'ul' | 'all'.

    Правила: упоминание ИП в любом месте делает категорию «ИП» (это отдельная
    группа льготников); иначе побеждает то упоминание, которое встретилось
    раньше — в формулировках ФНС ведущее слово стоит первым
    («Организации инвалидов» -> ЮЛ, «Инвалиды I группы» -> ФЛ).
    """
    blob = stem_phrase(text)
    if not blob:
        return "all"
    padded = f" {blob} "
    positions: dict[str, int] = {}
    for payer, phrases in _payer_stems():
        for phrase in phrases:
            if not phrase:
                continue
            pos = padded.find(f" {phrase} ")
            if pos == -1 and padded.find(f" {phrase}") != -1:
                pos = padded.find(f" {phrase}")
            if pos != -1:
                positions[payer] = min(positions.get(payer, pos), pos)
    if not positions:
        return "all"
    if "ip" in positions:
        return "ip"
    return min(positions, key=positions.get)


def detect_payer(*texts: object) -> str:
    """Определяет категорию по первому тексту, который её однозначно задаёт."""
    for text in texts:
        payer = detect_payer_single(text)
        if payer != "all":
            return payer
    return "all"


# ------------------------------------------------------ стеммер (Snowball, RU)

_VOWELS = set("аеиоуыэюя")

_PERFECTIVE_GERUND_1 = ("вшись", "вши", "в")
_PERFECTIVE_GERUND_2 = ("ившись", "ывшись", "ивши", "ывши", "ив", "ыв")
_ADJECTIVE = (
    "ими", "ыми", "его", "ого", "ему", "ому", "ее", "ие", "ые", "ое", "ей",
    "ий", "ый", "ой", "ем", "им", "ым", "ом", "их", "ых", "ую", "юю", "ая",
    "яя", "ою", "ею",
)
_PARTICIPLE_1 = ("ющ", "нн", "вш", "ем", "щ")
_PARTICIPLE_2 = ("ивш", "ывш", "ующ")
_REFLEXIVE = ("ся", "сь")
_VERB_1 = (
    "ешь", "нно", "ете", "йте", "ла", "на", "ли", "ем", "ло", "но", "ет",
    "ют", "ны", "ть", "й", "л", "н",
)
_VERB_2 = (
    "ейте", "уйте", "ила", "ыла", "ена", "ите", "или", "ыли", "ило", "ыло",
    "ено", "ует", "уют", "ены", "ить", "ыть", "ишь", "ей", "уй", "ил", "ыл",
    "им", "ым", "ен", "ят", "ит", "ыт", "ую", "ю",
)
_NOUN = (
    "иями", "ями", "ами", "иях", "ией", "иям", "ием", "ях", "ах", "ов", "ев",
    "ие", "ье", "еи", "ии", "ей", "ой", "ий", "ям", "ем", "ам", "ом", "ию",
    "ью", "ия", "ья", "а", "е", "и", "й", "о", "у", "ы", "ь", "ю", "я",
)
_DERIVATIONAL = ("ость", "ост")
_SUPERLATIVE = ("ейше", "ейш")


def _rv_r2(word: str) -> tuple[int, int]:
    rv = len(word)
    for i, ch in enumerate(word):
        if ch in _VOWELS:
            rv = i + 1
            break
    r1 = len(word)
    for i in range(1, len(word)):
        if word[i] not in _VOWELS and word[i - 1] in _VOWELS:
            r1 = i + 1
            break
    r2 = len(word)
    for i in range(r1 + 1, len(word)):
        if word[i] not in _VOWELS and word[i - 1] in _VOWELS:
            r2 = i + 1
            break
    return rv, r2


def _strip_ending(word: str, rv: int, endings: Iterable[str]) -> str | None:
    for ending in sorted(endings, key=len, reverse=True):
        if word.endswith(ending) and len(word) - len(ending) >= rv:
            return word[: len(word) - len(ending)]
    return None


@functools.lru_cache(maxsize=200_000)
def stem(word: str) -> str:
    """Стеммер Портера (Snowball) для русского языка."""
    word = word.lower().replace("ё", "е")
    if len(word) <= 3 or not any(ch in _VOWELS for ch in word):
        return word
    rv, r2 = _rv_r2(word)

    # шаг 1
    stemmed = _strip_ending(word, rv, _PERFECTIVE_GERUND_2)
    if stemmed is None:
        for ending in sorted(_PERFECTIVE_GERUND_1, key=len, reverse=True):
            if word.endswith(ending) and len(word) - len(ending) - 1 >= rv - 1:
                base = word[: len(word) - len(ending)]
                if base and base[-1] in "ая":
                    stemmed = base
                    break
    if stemmed is None:
        base = _strip_ending(word, rv, _REFLEXIVE) or word
        adj = _strip_ending(base, rv, _ADJECTIVE)
        if adj is not None:
            part = _strip_ending(adj, rv, _PARTICIPLE_2)
            if part is None:
                for ending in sorted(_PARTICIPLE_1, key=len, reverse=True):
                    if adj.endswith(ending) and len(adj) - len(ending) >= rv:
                        prev = adj[: len(adj) - len(ending)]
                        if prev and prev[-1] in "ая":
                            part = prev
                            break
            stemmed = part if part is not None else adj
        else:
            verb = _strip_ending(base, rv, _VERB_2)
            if verb is None:
                for ending in sorted(_VERB_1, key=len, reverse=True):
                    if base.endswith(ending) and len(base) - len(ending) >= rv:
                        prev = base[: len(base) - len(ending)]
                        if prev and prev[-1] in "ая":
                            verb = prev
                            break
            if verb is not None:
                stemmed = verb
            else:
                stemmed = _strip_ending(base, rv, _NOUN) or base
    word = stemmed

    # шаг 2: убрать «и» в RV
    if word.endswith("и") and len(word) - 1 >= rv:
        word = word[:-1]

    # шаг 3: производный суффикс в R2
    for ending in _DERIVATIONAL:
        if word.endswith(ending) and len(word) - len(ending) >= r2:
            word = word[: len(word) - len(ending)]
            break

    # шаг 4
    if word.endswith("нн"):
        word = word[:-1]
    else:
        sup = _strip_ending(word, rv, _SUPERLATIVE)
        if sup is not None:
            word = sup[:-1] if sup.endswith("нн") else sup
        elif word.endswith("ь"):
            word = word[:-1]
    return word


try:  # необязательная, но более точная лемматизация
    import pymorphy3  # type: ignore

    _MORPH = pymorphy3.MorphAnalyzer()

    @functools.lru_cache(maxsize=200_000)
    def normal_form(word: str) -> str:
        return _MORPH.parse(word)[0].normal_form

    LEMMATIZER = "pymorphy3"
except Exception:  # pragma: no cover - обычный путь без pymorphy3
    normal_form = stem
    LEMMATIZER = "snowball-stemmer"


# ------------------------------------------------------------------ стоп-слова

_TOKEN_RE = re.compile(r"[а-яa-z0-9][а-яa-z0-9-]*", re.IGNORECASE)


def tokenize(text: str) -> list[str]:
    lowered = clean(text).lower().replace("ё", "е")
    return [t for t in _TOKEN_RE.findall(lowered) if len(t) > 1]


def _read_wordlist(path: Path) -> list[str]:
    if not path.exists():
        return []
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            words.append(line)
    return words


@functools.lru_cache(maxsize=8)
def stopword_set(extra: tuple[str, ...] = ()) -> frozenset[str]:
    """Стоп-слова в нормализованной форме (общие + юридические клише)."""
    words: list[str] = []
    for name in ("stopwords_ru.txt", "stopwords_legal.txt"):
        words += _read_wordlist(config.REFERENCE_DIR / name)
    words += [w.lower() for w in extra]
    forms: set[str] = set()
    for word in words:
        forms.add(word)
        for token in tokenize(word):
            forms.add(token)
            forms.add(normal_form(token))
    return frozenset(forms)


def is_stopword(token: str, stops: frozenset[str]) -> bool:
    if token in stops:
        return True
    return normal_form(token) in stops


def terms(text: str, stops: frozenset[str], *, bigrams: bool = True) -> Iterator[tuple[str, str, bool]]:
    """Выдаёт (нормализованная_форма, исходная_словоформа, это_биграмма).

    Биграммы собираются только из соседних значимых слов, поэтому в облаке
    появляются осмысленные словосочетания («многодетные семьи»).
    """
    tokens = tokenize(text)
    kept: list[tuple[str, str, int]] = []
    for pos, token in enumerate(tokens):
        if token.isdigit() or len(token) < 3:
            continue
        if is_stopword(token, stops):
            continue
        kept.append((normal_form(token), token, pos))
    for norm, surface, _pos in kept:
        yield norm, surface, False
    if bigrams:
        for (n1, s1, p1), (n2, s2, p2) in zip(kept, kept[1:]):
            if p2 == p1 + 1:
                yield f"{n1} {n2}", f"{s1} {s2}", True
