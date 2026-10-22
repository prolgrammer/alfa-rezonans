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
# хвост диапазона «3-5%»: единица стоит после второго числа
_RANGE_TAIL_RE = re.compile(r"-\s?\d+(?:[.,]\d+)?\s?")
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


def _whole_number_at(text: str, start: int, end: int) -> bool:
    # цитата «4%» не должна находиться внутри «14%», а «1,5» — внутри «1,55»
    if (
        text[start].isdigit()
        and start
        and (
            text[start - 1].isdigit() or (text[start - 1] in ".," and start > 1 and text[start - 2].isdigit())
        )
    ):
        return False
    after = text[end : end + 2]
    return not (
        text[end - 1].isdigit()
        and after
        and (after[0].isdigit() or (after[0] in ".," and after[1:].isdigit()))
    )


def _find(text: str, part: str, pos: int) -> int:
    found = text.find(part, pos)
    while found >= 0 and not _whole_number_at(text, found, found + len(part)):
        found = text.find(part, found + 1)
    return found


def _contains_in_order(text: str, parts: list[str]) -> bool:
    pos = _find(text, parts[0], 0)
    while pos >= 0:
        end, ok = pos + len(parts[0]), True
        for part in parts[1:]:
            found = _find(text, part, end)
            if found < 0 or found - end > QUOTE_GAP_MAX:
                ok = False
                break
            end = found + len(part)
        if ok:
            return True
        pos = _find(text, parts[0], pos + 1)
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


def unit_matches(value: Decimal, unit: str, quote: str) -> bool:
    """Значение стоит в цитате с тем же масштабом (тыс, млн, млрд, трлн), а процент — с «%».

    Без этого «1,5 трлн рублей» прошло бы как 1,5 млрд ₽.
    """
    unit, text = normalize(unit), normalize(quote)
    scale = _SCALE_RE.search(unit)
    want = scale.group(0) if scale else None
    for m in _NUMBER_RE.finditer(text):
        if Decimal(m.group(0).replace(" ", "").replace(",", ".")) != value:
            continue
        after = text[m.end() :].lstrip()
        after = after[_RANGE_TAIL_RE.match(after).end() :] if _RANGE_TAIL_RE.match(after) else after
        got = _SCALE_RE.match(after)
        if (got.group(0) if got else None) != want:
            continue
        if "%" in unit and not after.startswith(("%", "процент")):
            continue
        return True
    return False


def show_number(value: Decimal) -> str:
    return format(value.normalize(), "f")


def date_mentioned(date: dt.date, text: str) -> bool:
    text = normalize(text)
    day, month = date.day, date.month
    patterns = (
        rf"(?<!\d)0?{day} {_MONTHS[month - 1]}",
        # «1.5%» — дробь, а не 1 мая
        rf"(?<![\d.,])0?{day}\.0?{month}(?!\d)(?! ?%)",
        rf"(?<!\d){date.year}-{month:02d}-{day:02d}(?!\d)",
    )
    return any(re.search(p, text) for p in patterns)
