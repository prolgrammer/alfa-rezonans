import unicodedata

# Cf (в том числе теги U+E0000 и изоляты двунаправленного текста), Co и Cn прячут инструкции от глаза
# читающего; Cc тоже, кроме \n и \t, которые нужны абзацам. Список диапазонов вручную всегда отстаёт
# от Юникода, поэтому отбираем по категории.
_DROPPED = frozenset({"Cf", "Cc", "Co", "Cn"})
_KEPT_CONTROLS = frozenset({"\n", "\t"})


def sanitize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(
        ch for ch in normalized if ch in _KEPT_CONTROLS or unicodedata.category(ch) not in _DROPPED
    )
