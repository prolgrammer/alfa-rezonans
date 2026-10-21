import html
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from html.parser import HTMLParser

# ML-10: результат инструмента агента в промпте — не длиннее 1500 символов
TOOL_RESULT_LIMIT = 1500

# Cf (в том числе теги U+E0000 и изоляты двунаправленного текста), Co, Cn и Cs прячут инструкции от глаза
# читающего или ломают запись в UTF-8; Cc тоже, кроме \n и \t, которые нужны абзацам. Список диапазонов
# вручную всегда отстаёт от Юникода, поэтому отбираем по категории. Селекторы вариантов (Mn) в категорию
# не попадают, их убираем по диапазонам.
_DROPPED = frozenset({"Cf", "Cc", "Co", "Cn", "Cs"})
_KEPT_CONTROLS = frozenset({"\n", "\t"})
_VARIATION_SELECTORS = (range(0xFE00, 0xFE10), range(0xE0100, 0xE01F0))

# NFKC сводит полноширинные «＜／source＞» и лигатуры к обычным символам, но портит и русский деловой текст:
# «№ 199» → «No 199», «м²» → «м2», «…» → «...». Эти знаки в инъекциях не опасны — оставляем как есть.
_KEPT_COMPAT = frozenset("№…²³¼½¾™")
_KEPT_COMPAT_RE = re.compile(f"([{''.join(sorted(_KEPT_COMPAT))}])")

# содержимое этих тегов читатель не видит, а модель увидела бы
_HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "blockquote", "section", "article", "header"}
    | {"footer", "hr", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
)
# сущность может раскрыться в тег (&lt;b&gt;), тег — в сущность (&amp;lt;): повторяем до неподвижной точки
_MAX_PASSES = 4


class Flag(StrEnum):
    SOURCE_TAG = "source_tag"
    MARKUP_TAG = "markup_tag"
    BASE64 = "base64"
    COMMAND = "command"


@dataclass(frozen=True)
class Prepared:
    text: str
    flags: frozenset[Flag]


@dataclass(frozen=True)
class Source:
    text: str
    attrs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Wrapped:
    n: int | str
    text: str
    flags: frozenset[Flag]
    truncated: bool


@dataclass(frozen=True)
class Rendered:
    text: str
    flags: frozenset[Flag]
    blocks: tuple[Wrapped, ...]


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


def _nfkc(text: str) -> str:
    return "".join(
        part if part in _KEPT_COMPAT else unicodedata.normalize("NFKC", part)
        for part in _KEPT_COMPAT_RE.split(text)
    )


def _clean_chars(text: str) -> str:
    # Фильтр до нормализации, чтобы скрытый символ между буквой и диакритикой не мешал им склеиться;
    # фильтр после — на случай, если NFKC сам породил запрещённый символ.
    return _strip(_nfkc(_strip(text)))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden = max(0, self._hidden - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _strip_html(text: str) -> str:
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


_RUN_OF_SPACES = re.compile(r" {2,}")


def _normalize_spaces(text: str) -> str:
    lines = [_RUN_OF_SPACES.sub(" ", line).strip(" ") for line in text.split("\n")]
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept).strip("\n")


def _sanitize_once(text: str) -> str:
    return _normalize_spaces(_clean_chars(_strip_html(_clean_chars(text))))


def sanitize(text: str) -> str:
    for _ in range(_MAX_PASSES):
        cleaned = _sanitize_once(text)
        if cleaned == text:
            break
        text = cleaned
    return text


# --- подготовка к промпту ---------------------------------------------------------------------------

# Любое «<source», «</ source» и т. п. — граница нашего блока, поэтому без \b: «<sources» тоже.
# Остальные имена — разметка ролей и шаблонов чата, которую модель может принять за служебную.
_TAG_RE = re.compile(
    r"<(?P<head>\s*/?\s*(?:(?P<source>source)|(?:system|user|assistant|human|developer|instructions?|prompt"
    r"|tool|im_start|im_end|endoftext)\b))(?P<rest>[^<>\n]{0,80})(?P<close>>)?",
    re.I,
)
_CHAT_TOKEN_RE = re.compile(r"<(\|[^<>|\n]{1,40}\|)>")
_LLAMA_RE = re.compile(r"\[(/?INST)\]|<<(/?SYS)>>", re.I)

# 64+ символов алфавита base64 без пробелов, строки MIME-переноса склеиваются в одну серию. Слаг ссылки
# и hex-хэш — в одном регистре, поэтому условие «есть заглавная, строчная и цифра» их не трогает.
_B64_RE = re.compile(r"[A-Za-z0-9+/_-]{64,}={0,2}(?:[ \t]*\n[ \t]*[A-Za-z0-9+/_-]{16,}={0,2})*")
_B64_MARK = "[base64 опущен: {} символов]"

COMMAND_MARK = "[!команда] "

# Похожие буквы другого алфавита: «игнoрируй» с латинской o должна совпасть с русским шаблоном.
# Замена посимвольная — позиции в «скелете» совпадают с позициями в тексте.
_TO_CYRILLIC = str.maketrans("aeopcxykmthbAEOPCXYKMTHBё", "аеорсхукмтнвАЕОРСХУКМТНВе")
_TO_LATIN = str.maketrans("аеорсхукмтнвАЕОРСХУКМТНВ", "aeopcxykmthbAEOPCXYKMTHB")

_RU_OBJECT = r"(?:инструкци|указани|промпт|подсказк)\w*"
_RU_QUALIFIED_OBJECT = (
    r"(?:все\w*|предыдущ\w*|прошл\w*|вышеуказ\w*|вышеизлож\w*|ранее|прежн\w*|систем\w*|сво\w*|тво\w*)"
    r"\s+(?:\w+\s+){0,2}?(?:правил|ограничени|команд|настройк)\w*"
)
_RU_PATTERNS = [
    r"\b(?:игнорируй|забудь|отмени|отбрось|пропусти|не\s+учитывай|не\s+следуй|не\s+выполняй)(?:те)?\s+"
    rf"(?:\w+\s+){{0,3}}?(?:{_RU_OBJECT}|{_RU_QUALIFIED_OBJECT})",
    r"\b(?:ты\s+теперь|теперь\s+ты|отныне\s+ты|с\s+этого\s+момента\s+ты)\b",
    r"\b(?:нов|обновленн|настоящ|секретн|скрыт)\w*\s+инструкци\w*\s*:",
    r"\bсистемн\w*\s+(?:промпт|сообщени|инструкци|подсказк)\w*",
    r"\b(?:покажи|выведи|раскрой|напечатай|повтори|перескажи)(?:те)?\s+(?:\w+\s+){0,2}?"
    r"(?:промпт|инструкци)\w*",
    r"\b(?:поставь|присвой|выстави|назначь)(?:те)?\s+(?:\w+\s+){0,3}?"
    r"(?:оценк|релевантност|балл|приоритет|рейтинг)\w*",
    r"\b(?:отвечай|ответь)(?:те)?\s+(?:\w+\s+){0,2}?(?:только|лишь|исключительно)\b",
    r"\b(?:считай|признай|отметь|пометь)(?:те)?\s+(?:\w+\s+){0,2}?(?:новост|событи|публикаци|текст)\w*\s+"
    r"(?:\w+\s+){0,1}?(?:важн|релевантн|делов|бизнес)\w*",
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:систем\w*|ассистент|пользователь)[ \t]*:",
    r"^[ \t]*#{1,6}[ \t]*(?:систем\w*|инструкци\w*)\b",
]
_EN_PATTERNS = [
    r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:\w+\s+){0,3}?"
    r"(?:instructions?|prompts?|directions|guidelines|rules|context)\b",
    r"\byou\s+are\s+now\b|\bfrom\s+now\s+on,?\s+you\b|\bpretend\s+(?:to\s+be|you\s+are)\b",
    r"\b(?:act|behave)\s+as\s+(?:if\s+you|an?\s+(?:ai|assistant|model|chatbot|system))\b",
    r"\b(?:new|updated|real|actual|hidden|secret)\s+instructions?\s*:",
    r"\bsystem\s+(?:prompt|message|instructions?)\b",
    r"\b(?:reveal|print|show|output|repeat|display)\s+(?:\w+\s+){0,2}?(?:prompt|instructions)\b",
    r"\b(?:respond|answer|reply)\s+(?:\w+\s+){0,2}?only\b",
    r"\bset\s+(?:\w+\s+){0,3}?(?:relevance|score|rating|priority)\b",
    r"\b(?:mark|classify|treat|label)\s+(?:this|the)\s+(?:news|article|item|text|story)\s+as\b",
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:system|assistant|user|developer)[ \t]*:",
    r"^[ \t]*#{1,6}[ \t]*(?:system|instructions?)\b",
]
_RU_RE = re.compile("|".join(f"(?:{p})" for p in _RU_PATTERNS), re.I | re.M)
_EN_RE = re.compile("|".join(f"(?:{p})" for p in _EN_PATTERNS), re.I | re.M)


def _skeleton(text: str, table: dict[int, str]) -> str:
    # str.lower() у пары символов (İ) меняет длину — такие оставляем как есть
    return "".join(low if len(low := ch.lower()) == 1 else ch for ch in text.translate(table))


def _defuse_tags(text: str, flags: set[Flag]) -> str:
    def tag(m: re.Match[str]) -> str:
        flags.add(Flag.SOURCE_TAG if m.group("source") else Flag.MARKUP_TAG)
        return f"‹{m.group('head')}{m.group('rest')}{'›' if m.group('close') else ''}"

    def chat_token(m: re.Match[str]) -> str:
        flags.add(Flag.MARKUP_TAG)
        return f"‹{m.group(1)}›"

    def llama(m: re.Match[str]) -> str:
        flags.add(Flag.MARKUP_TAG)
        return f"‹{m.group(1)}›" if m.group(1) else f"‹‹{m.group(2)}››"

    text = _TAG_RE.sub(tag, text)
    text = _CHAT_TOKEN_RE.sub(chat_token, text)
    return _LLAMA_RE.sub(llama, text)


def _drop_base64(text: str, flags: set[Flag]) -> str:
    def repl(m: re.Match[str]) -> str:
        run = m.group(0)
        if not (re.search(r"[A-Z]", run) and re.search(r"[a-z]", run) and re.search(r"[0-9]", run)):
            return run
        flags.add(Flag.BASE64)
        return _B64_MARK.format(sum(not ch.isspace() for ch in run))

    return _B64_RE.sub(repl, text)


def _mark_commands(text: str, flags: set[Flag]) -> str:
    starts: list[int] = []
    for pattern, table in ((_RU_RE, _TO_CYRILLIC), (_EN_RE, _TO_LATIN)):
        for m in pattern.finditer(_skeleton(text, table)):
            start = m.start()
            while start < m.end() and text[start].isspace():
                start += 1
            starts.append(start)
    found = False
    for start in sorted(set(starts), reverse=True):
        if text[max(0, start - len(COMMAND_MARK)) : start] == COMMAND_MARK:
            flags.add(Flag.COMMAND)
            continue
        text = text[:start] + COMMAND_MARK + text[start:]
        found = True
    if found:
        flags.add(Flag.COMMAND)
    return text


def neutralize(text: str) -> Prepared:
    flags: set[Flag] = set()
    # HTML-разбор в sanitize молча убирает настоящие </source> и <<SYS>>; попытку видно по сырому тексту
    _defuse_tags(html.unescape(_clean_chars(text)), flags)
    text = _defuse_tags(sanitize(text), flags)
    text = _drop_base64(text, flags)
    text = _mark_commands(text, flags)
    return Prepared(text, frozenset(flags))


# --- обёртка <source> ---------------------------------------------------------------------------------

TRUNCATED_MARK = " … [обрезано]"
_N_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")
_ATTR_NAME_RE = re.compile(r"[a-z_]{1,20}")
_ATTR_VALUE_MAX = 80
# доля лимита, в которой ищем пробел для обрезки по слову
_WORD_CUT_WINDOW = 0.2


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    room = limit - len(TRUNCATED_MARK)
    cut = text[:room]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space >= room * (1 - _WORD_CUT_WINDOW):
        cut = cut[:space]
    return cut.rstrip() + TRUNCATED_MARK, True


def _attr_value(value: str) -> str:
    cleaned = sanitize(str(value)).replace("\n", " ").replace("\t", " ")
    cleaned = re.sub(r"[\"<>&]", "", cleaned)
    return _RUN_OF_SPACES.sub(" ", cleaned).strip()[:_ATTR_VALUE_MAX].strip()


def wrap_source(n: int | str, text: str, /, *, limit: int | None = None, **attrs: str) -> Wrapped:
    if isinstance(n, bool) or not (
        (isinstance(n, int) and n >= 1) or (isinstance(n, str) and _N_RE.fullmatch(n))
    ):
        raise ValueError(f"номер источника должен быть целым ≥ 1 или id вида E7, получено {n!r}")
    if limit is not None and limit <= len(TRUNCATED_MARK):
        raise ValueError(f"лимит источника слишком мал: {limit}")
    head = [f'n="{n}"']
    for name, value in attrs.items():
        if name == "n" or not _ATTR_NAME_RE.fullmatch(name):
            raise ValueError(f"недопустимое имя атрибута источника: {name!r}")
        head.append(f'{name}="{_attr_value(value)}"')
    prepared = neutralize(text)
    body, truncated = (prepared.text, False) if limit is None else _truncate(prepared.text, limit)
    block = (
        f"<source {' '.join(head)}>\n{body}\n</source>" if body else f"<source {' '.join(head)}>\n</source>"
    )
    return Wrapped(n=n, text=block, flags=prepared.flags, truncated=truncated)


def render_sources(items: Iterable[str | Source], *, limit: int | None = None, start: int = 1) -> Rendered:
    blocks = tuple(
        wrap_source(n, item, limit=limit)
        if isinstance(item, str)
        else wrap_source(n, item.text, limit=limit, **item.attrs)
        for n, item in enumerate(items, start)
    )
    flags = frozenset().union(*(b.flags for b in blocks))
    return Rendered(text="\n\n".join(b.text for b in blocks), flags=flags, blocks=blocks)


def tool_result(n: int | str, tool: str, payload: str, /) -> Wrapped:
    return wrap_source(n, payload, limit=TOOL_RESULT_LIMIT, tool=tool)
