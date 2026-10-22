import datetime as dt
import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal

from app.text.sanitize import strip_marks

# короче этого под цитату подходит что угодно: «ставка», «14%»
QUOTE_MIN = 10

_QUOTES = str.maketrans(dict.fromkeys("«»„“”‟″‘’‚‛`´'", '"'))
_DASHES = str.maketrans(dict.fromkeys("‐‑‒–—―−", "-"))
_SPACES_RE = re.compile(r"\s+")
# NFKC превращает «…» в три точки: пропуск в цитате ищем по ним
_ELLIPSIS_RE = re.compile(r"\.{3,}")
_EDGE = ' ".,;:!?-'
# разряды через пробел (`1 500`) — одно число; NFKC уже свёл неразрывные и узкие пробелы к обычному
_NUMBER_RE = re.compile(r"(?<![\d.,])(?:\d{1,3}(?: \d{3})+(?![\d])|\d+)(?:[.,]\d+)?")
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    text = text.translate(_QUOTES).translate(_DASHES)
    return _SPACES_RE.sub(" ", text).strip()


def _parts(quote: str) -> list[str]:
    parts = (p.strip(_EDGE) for p in _ELLIPSIS_RE.split(normalize(strip_marks(quote))))
    return [p for p in parts if p]


def _contains_in_order(text: str, parts: list[str]) -> bool:
    pos = 0
    for part in parts:
        found = text.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    return True


def find_quote(quote: str, texts: Mapping[int, str]) -> set[int]:
    """Номера текстов, в которых цитата есть дословно с точностью до нормализации.

    Тексты — в том виде, какой видела модель: пометки санитайзера снимаются и с них, и с цитаты.
    """
    parts = _parts(quote)
    if sum(len(p) for p in parts) < QUOTE_MIN:
        return set()
    return {n for n, text in texts.items() if _contains_in_order(normalize(strip_marks(text)), parts)}


def numbers(text: str) -> set[Decimal]:
    # знак не учитываем: «снизил на 1 п. п.» и «−1» — одно число
    found = _NUMBER_RE.findall(unicodedata.normalize("NFKC", text))
    return {Decimal(n.replace(" ", "").replace(",", ".")) for n in found}


def show_number(value: Decimal) -> str:
    return format(value.normalize(), "f")


def date_mentioned(date: dt.date, text: str) -> bool:
    text = normalize(text)
    day, month = date.day, date.month
    patterns = (
        rf"(?<!\d)0?{day} {_MONTHS[month - 1]}",
        rf"(?<![\d.,])0?{day}\.0?{month}(?!\d)",
        rf"(?<!\d){date.year}-{month:02d}-{day:02d}(?!\d)",
    )
    return any(re.search(p, text) for p in patterns)
