import datetime as dt
import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal

from app.text.sanitize import strip_marks

# короче этого под цитату подходит что угодно: «ставка», «14%»
QUOTE_MIN = 10
# пропуск «…» в цитате — внутри одной мысли, а не склейка слов из разных абзацев
QUOTE_GAP_MAX = 200

_QUOTES = str.maketrans(dict.fromkeys("«»„“”‟″‘’‚‛`´'", '"'))
_DASHES = str.maketrans(dict.fromkeys("‐‑‒–—―−", "-"))
_SPACES_RE = re.compile(r"\s+")
# NFKC превращает «…» в три точки: пропуск в цитате ищем по ним
_ELLIPSIS_RE = re.compile(r"\.{3,}")
_EDGE = ' ".,;:!?-'
# разряды через пробел (`1 500`) — одно число; NFKC уже свёл неразрывные и узкие пробелы к обычному.
# Цифра сразу после буквы — не число: «м²» после NFKC даёт «м2», «Qwen3» — название.
_NUMBER_RE = re.compile(r"(?<![\d.,])(?<![^\W\d_])(?:\d{1,3}(?: \d{3})+(?![\d])|\d+)(?:[.,]\d+)?")
_SCALE_RE = re.compile(r"тыс|млн|млрд|трлн")
# полные слова масштаба сводим к сокращениям: «1,5 миллиарда» — то же, что «1,5 млрд»
_SCALE_WORDS = (
    (re.compile(r"миллиард\w*"), "млрд"),
    (re.compile(r"триллион\w*"), "трлн"),
    (re.compile(r"миллион\w*"), "млн"),
    (re.compile(r"тысяч\w*"), "тыс"),
)
# хвост диапазона «3-5%»: единица стоит после второго числа
_RANGE_TAIL_RE = re.compile(r"-\s?\d+(?:[.,]\d+)?\s?")
# «процентный пункт» — не процент: «на 1 процентный пункт» не проходит как 1%
_PERCENT_RE = re.compile(r"%|процент(?!н)")
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


def _number_bounds(text: str) -> tuple[set[int], set[int]]:
    spans = [m.span() for m in _NUMBER_RE.finditer(text)]
    return {a for a, _ in spans}, {b for _, b in spans}


def _find(text: str, part: str, pos: int, bounds: tuple[set[int], set[int]]) -> int:
    # часть, которая начинается или кончается цифрой, совпадает с границей целого числа источника:
    # «4%» не находится внутри «14%», «500 млн» — внутри «1 500 млн», «1,5» — внутри «1,55»
    starts, ends = bounds
    found = text.find(part, pos)
    while found >= 0 and (
        (part[0].isdigit() and found not in starts) or (part[-1].isdigit() and found + len(part) not in ends)
    ):
        found = text.find(part, found + 1)
    return found


def _contains_in_order(text: str, parts: list[str]) -> bool:
    bounds = _number_bounds(text)
    pos = _find(text, parts[0], 0, bounds)
    while pos >= 0:
        end, ok = pos + len(parts[0]), True
        for part in parts[1:]:
            found = _find(text, part, end, bounds)
            if found < 0 or found - end > QUOTE_GAP_MAX:
                ok = False
                break
            end = found + len(part)
        if ok:
            return True
        pos = _find(text, parts[0], pos + 1, bounds)
    return False


def quote_size(quote: str) -> int:
    """Длина цитаты без пометок, пропусков и краевой пунктуации — то, что реально сверяется."""
    return sum(len(p) for p in _parts(quote))


def find_quote(quote: str, texts: Mapping[int, str], *, min_size: int = QUOTE_MIN) -> set[int]:
    """Номера текстов, в которых цитата есть дословно с точностью до нормализации.

    Тексты — в том виде, какой видела модель: пометки санитайзера снимаются и с них, и с цитаты.
    Цитата с пропуском «…» собирается только из кусков не короче QUOTE_MIN: иначе из коротких слов
    источника можно сложить обратный ему смысл.
    """
    parts = _parts(quote)
    if not parts or sum(len(p) for p in parts) < min_size:
        return set()
    if len(parts) > 1 and min(len(p) for p in parts) < QUOTE_MIN:
        return set()
    return {n for n, text in texts.items() if _contains_in_order(normalize(strip_marks(text)), parts)}


def numbers(text: str) -> set[Decimal]:
    # знак не учитываем: «снизил на 1 п. п.» и «−1» — одно число
    found = _NUMBER_RE.findall(unicodedata.normalize("NFKC", text))
    return {Decimal(n.replace(" ", "").replace(",", ".")) for n in found}


def _short_scales(text: str) -> str:
    for pattern, short in _SCALE_WORDS:
        text = pattern.sub(short, text)
    return text


type NumberMark = tuple[Decimal, str | None, bool]


def number_marks(text: str) -> set[NumberMark]:
    """Числа текста с масштабом (тыс, млн, млрд, трлн) и признаком процента после них.

    Число без масштаба и «1,5 млрд» — разные отметки: так «1,5 трлн рублей» не пройдёт как 1,5 млрд ₽.
    """
    text = _short_scales(normalize(text))
    marks: set[NumberMark] = set()
    for m in _NUMBER_RE.finditer(text):
        after = text[m.end() :].lstrip()
        if tail := _RANGE_TAIL_RE.match(after):
            after = after[tail.end() :]
        scale = _SCALE_RE.match(after)
        value = Decimal(m.group(0).replace(" ", "").replace(",", "."))
        marks.add((value, scale.group(0) if scale else None, bool(_PERCENT_RE.match(after))))
    return marks


def unit_matches(value: Decimal, unit: str, quote: str) -> bool:
    """Значение стоит в цитате с масштабом из единицы, а процент — с «%» (в обе стороны: «5%» — не 5 ₽)."""
    unit = _short_scales(normalize(unit))
    scale = _SCALE_RE.search(unit)
    want = (value, scale.group(0) if scale else None, bool(_PERCENT_RE.search(unit)))
    return want in number_marks(quote)


def clip(text: str, limit: int) -> str:
    """Схлопывает пробелы и обрезает по слову с «…»: префикс дословной цитаты остаётся дословным."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def show_number(value: Decimal) -> str:
    return format(value.normalize(), "f")


def date_mentioned(date: dt.date, text: str) -> bool:
    text = normalize(text)
    day, month = date.day, date.month
    patterns = (
        rf"(?<!\d)0?{day} {_MONTHS[month - 1]}",
        # «1.5%» — дробь, а не 1 мая
        rf"(?<![\d.,])0?{day}\.0?{month}(?!\d)(?! ?(?:%|процент))",
        rf"(?<!\d){date.year}-{month:02d}-{day:02d}(?!\d)",
    )
    return any(re.search(p, text) for p in patterns)
