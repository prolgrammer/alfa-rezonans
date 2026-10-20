import re
import unicodedata

# \n и \t нужны абзацам; остальные управляющие и «невидимые» символы прячут инструкции от глаза читающего
_STRIP = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤﻿]")


def sanitize(text: str) -> str:
    return _STRIP.sub("", unicodedata.normalize("NFKC", text))
