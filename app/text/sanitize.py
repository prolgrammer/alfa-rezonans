import unicodedata

# Cf (в том числе теги U+E0000 и изоляты двунаправленного текста), Co, Cn и Cs прячут инструкции от глаза
# читающего или ломают запись в UTF-8; Cc тоже, кроме \n и \t, которые нужны абзацам. Список диапазонов
# вручную всегда отстаёт от Юникода, поэтому отбираем по категории. Селекторы вариантов (Mn) в категорию
# не попадают, их убираем по диапазонам.
_DROPPED = frozenset({"Cf", "Cc", "Co", "Cn", "Cs"})
_KEPT_CONTROLS = frozenset({"\n", "\t"})
_VARIATION_SELECTORS = (range(0xFE00, 0xFE10), range(0xE0100, 0xE01F0))


def _strip(text: str) -> str:
    return "".join(
        ch
        for ch in text
        if ch in _KEPT_CONTROLS
        or (
            unicodedata.category(ch) not in _DROPPED
            and not any(ord(ch) in selectors for selectors in _VARIATION_SELECTORS)
        )
    )


def sanitize(text: str) -> str:
    # Фильтр до нормализации, чтобы скрытый символ между буквой и диакритикой не мешал им склеиться;
    # фильтр после — на случай, если NFKC сам породил запрещённый символ. Итог идемпотентен.
    return _strip(unicodedata.normalize("NFKC", _strip(text)))
