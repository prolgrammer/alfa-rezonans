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
# одиночный \r, \v, \f, NEL и разделители строк Юникода — тоже границы строк: удалить их — склеить слова
_LINE_BREAKS = frozenset({"\r", "\v", "\f", chr(0x85), chr(0x2028), chr(0x2029)})
# кэш решений по символам ограничен, чтобы текст из миллиона разных символов не раздувал память
_CACHED_BELOW = 0x20000

# NFKC сводит полноширинные «＜／source＞» и лигатуры к обычным символам, но портит и русский деловой текст:
# «№ 199» → «No 199», «м²» → «м2», «…» → «...». Эти знаки в инъекциях не опасны — оставляем как есть.
_KEPT_COMPAT = frozenset("№…²³¼½¾™")
_KEPT_COMPAT_RE = re.compile(f"([{''.join(sorted(_KEPT_COMPAT))}])")

# Тегом считаем только элемент HTML: «акции <Alfa Group>», «x<y» и «<system>» остаются текстом, и тогда
# neutralize видит и помечает псевдотеги даже в тексте, сохранённом ролью 3. <source> — только с атрибутами
# медиа: голый <source> и </source> в новости — это попытка закрыть наш блок, а не разметка.
_HTML_TAGS = (
    "a abbr address area article aside audio b base bdi bdo big blockquote body br button canvas caption "
    "center cite code col colgroup data datalist dd del details dfn dialog div dl dt em embed fieldset "
    "figcaption figure font footer form frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe "
    "img input ins kbd label legend li link main map mark marquee menu meta meter nav nobr noframes noscript "
    "object ol optgroup option output p param picture pre progress q rp rt ruby s samp script section select "
    "slot small span strike strong style sub summary sup svg table tbody td template textarea tfoot th thead "
    "time title tr track tt u ul var video wbr"
).split()
_NOT_MARKUP_RE = re.compile(
    rf"<(?!/?(?:{'|'.join(sorted(_HTML_TAGS, key=len, reverse=True))})\b[^<>]*>"
    # <o:p> и подобные — разметка Word с пространством имён, частая в пресс-релизах
    r"|/?[a-z]+:[a-z][\w-]*\b[^<>]*>"
    r"|source\s+(?:srcset|src|type|media|sizes)\b[^<>]*>|!--|![a-z][^<>]*>)",
    re.I,
)
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source"}
    | {"track", "wbr", "frame"}
)
# содержимое этих тегов читатель не видит, а модель увидела бы
_HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript"})
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "blockquote", "section", "article", "header"}
    | {"footer", "hr", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt", "figcaption", "main", "aside"}
)
_HIDDEN_NAMES = "|".join(sorted(_HIDDEN_TAGS))
# имя тега в HTML кончается пробелом, «/» или «>»: <scripts> и <script-x> — не <script>
_HIDDEN_OPEN_RE = re.compile(rf"<(?=({_HIDDEN_NAMES})[\s/>])", re.I)
_HIDDEN_CLOSE_RE = re.compile(rf"</({_HIDDEN_NAMES})[\s/>]", re.I)
# тот же элемент закрывает предыдущий: <li>…<li>, <td>…<td>
_IMPLICITLY_CLOSED = frozenset({"p", "li", "dt", "dd", "option", "tr", "td", "th"})
# теги, которые по спецификации HTML закрывают открытый <p>; table — нет: зависит от режима документа,
# а при сомнении скрытое должно остаться скрытым
_CLOSES_P = frozenset(
    {"address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset", "figcaption", "figure"}
    | {"footer", "header", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dd", "dt", "main", "menu", "nav"}
    | {"ol", "p", "pre", "section", "summary", "ul"}
)
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
# Сущность раскрывается в тег (&lt;b&gt;), тег — в сущность (&amp;lt;): повторяем до неподвижной точки.
# Каждый проход, который что-то меняет, укорачивает текст; предел — только страховка.
_MAX_PASSES = 100


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


class _CharFilter(dict[int, int | str | None]):
    # str.translate с таким словарём идёт по тексту в C; решение по символу считается один раз
    def __missing__(self, code: int) -> int | str | None:
        ch = chr(code)
        if ch in _LINE_BREAKS:
            value: int | str | None = "\n"
        elif ch in _KEPT_CONTROLS:
            value = code
        elif unicodedata.category(ch) in _DROPPED or any(code in r for r in _VARIATION_SELECTORS):
            value = None
        else:
            value = code
        if code < _CACHED_BELOW:
            self[code] = value
        return value


_CHAR_FILTER = _CharFilter()


def _strip(text: str) -> str:
    return text.replace("\r\n", "\n").translate(_CHAR_FILTER)


def _nfkc(text: str) -> str:
    return "".join(
        part if part in _KEPT_COMPAT else unicodedata.normalize("NFKC", part)
        for part in _KEPT_COMPAT_RE.split(text)
    )


def _clean_chars(text: str) -> str:
    # Фильтр до нормализации, чтобы скрытый символ между буквой и диакритикой не мешал им склеиться;
    # фильтр после — на случай, если NFKC сам породил запрещённый символ.
    return _strip(_nfkc(_strip(text)))


def _hidden_by_attrs(attrs: list[tuple[str, str | None]]) -> bool:
    values = dict(attrs)
    return (
        "hidden" in values
        or (values.get("aria-hidden") or "").strip().lower() == "true"
        or bool(_HIDDEN_STYLE_RE.search(values.get("style") or ""))
    )


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open: list[tuple[str, bool]] = []
        # сколько открыто элементов каждого имени: лишний закрывающий тег не сканирует стек — без квадрата
        self._count: dict[str, int] = {}
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag in _VOID_TAGS:
            return
        # браузер закрывает <p> перед новым блоком и <li> перед следующим <li>: иначе скрытый абзац
        # «<p hidden>…<p>текст» спрятал бы и видимый текст
        if (
            self._open
            and (top := self._open[-1][0]) in _IMPLICITLY_CLOSED
            and (top == tag or (top == "p" and tag in _CLOSES_P))
        ):
            self._close_to(len(self._open) - 1)
        hidden = tag in _HIDDEN_TAGS or _hidden_by_attrs(attrs)
        self._open.append((tag, hidden))
        self._count[tag] = self._count.get(tag, 0) + 1
        self._hidden += hidden

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # браузер не видит «/» у обычного элемента: <div hidden/> открывает скрытый блок
        self.handle_starttag(tag, attrs)
        if tag in _HIDDEN_TAGS:
            self.set_cdata_mode(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if not self._count.get(tag):
            return
        # HTML новостей часто без закрывающих </p>: закрываем до ближайшего открытого тега с тем же именем
        for i in range(len(self._open) - 1, -1, -1):
            if self._open[i][0] == tag:
                self._close_to(i)
                break

    def _close_to(self, index: int) -> None:
        for name, hidden in self._open[index:]:
            self._count[name] -= 1
            self._hidden -= hidden
        del self._open[index:]

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _keep_unclosed_hidden_as_text(text: str) -> str:
    # «добавляют тег <script> на страницу…» — упоминание, а не разметка: без закрывающего тега дальше
    # браузер спрятал бы всю новость. Открывающий тег после последнего закрывающего — текст.
    # позиции — по исходному тексту, все замены одним проходом: замена удлиняет текст и сдвигала бы их
    last_close: dict[str, int] = {}
    for m in _HIDDEN_CLOSE_RE.finditer(text):
        last_close[m.group(1).lower()] = m.start()
    return _HIDDEN_OPEN_RE.sub(
        lambda m: "<" if m.start() < last_close.get(m.group(1).lower(), -1) else "&lt;", text
    )


def _strip_html(text: str) -> str:
    text = _keep_unclosed_hidden_as_text(_NOT_MARKUP_RE.sub("&lt;", text))
    # незакрытый комментарий съел бы весь хвост новости — после последнего «-->» он остаётся текстом
    tail = text.rfind("-->") + 1
    text = text[:tail] + text[tail:].replace("<!--", "&lt;!--")
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

# Похожие буквы другого алфавита: «<ѕource» с кириллической ѕ и «игнoрируй» с латинской o должны совпасть
# с шаблонами. Замена посимвольная — позиции в «скелете» совпадают с позициями в тексте.
_TO_LATIN = str.maketrans(
    "аеорсхукмтнвіѕјАЕОРСХУКМТНВІЅЈοαειινρτυχκΟΑΕΙΚΝΡΤΥΧΒΜΗΖսօᴀʙᴄᴅᴇɢʜɪᴊᴋʟᴍɴᴏᴘʀꜱᴛᴜᴠᴡʏᴢ",
    "aeopcxykmthbisjAEOPCXYKMTHBISJoaeiivptuxkOAEIKNPTYXBMHZuoabcdeghijklmnoprstuvwyz",
)
_TO_CYRILLIC = str.maketrans(
    "aeopcxykmthbAEOPCXYKMTHBёЁοαερκτχΟΑΕΡΚΤΧΒΜΗ",
    "аеорсхукмтнвАЕОРСХУКМТНВеЕоаеркткОАЕРКТХВМН",
)


def _skeleton(text: str, table: dict[int, int]) -> str:
    low = text.translate(table).lower()
    if len(low) == len(text):
        return low
    # str.lower() у пары символов (İ) меняет длину — тогда посимвольно, такие оставляем как есть
    return "".join(c if len(c := ch.lower()) == 1 else ch for ch in text.translate(table))


# Любое «<source», «</ source» и т. п. — граница нашего блока, поэтому без \b: «<sources» тоже.
# Остальные имена — разметка ролей и шаблонов чата, которую модель может принять за служебную, и
# <script>/<style>, которые sanitize оставила текстом: браузер спрятал бы то, что идёт после них.
_TAG_RE = re.compile(
    r"<\s*/?\s*(?:(?P<source>source)|(?:system|user|assistant|human|developer|instructions?|prompt"
    r"|tool|im_start|im_end|endoftext|script|style|template|noscript)\b)[^<>\n]{0,80}>?",
    re.I,
)
_CHAT_TOKEN_RE = re.compile(r"<\|[^<>|\n]{1,40}\|>")
_LLAMA_RE = re.compile(r"\[/?inst\]|<</?sys>>", re.I)
_DEFUSED = str.maketrans("<>[]", "‹›‹›")


def _defuse_tags(text: str, flags: set[Flag]) -> str:
    skeleton = _skeleton(text, _TO_LATIN)
    spans: list[tuple[int, int, str]] = []
    for m in _TAG_RE.finditer(skeleton):
        flags.add(Flag.SOURCE_TAG if m.group("source") else Flag.MARKUP_TAG)
        spans.append((m.start(), m.end(), "<>"))
    for regex in (_CHAT_TOKEN_RE, _LLAMA_RE):
        for m in regex.finditer(skeleton):
            flags.add(Flag.MARKUP_TAG)
            spans.append((m.start(), m.end(), "<>[]"))
    if not spans:
        return text
    chars = list(text)
    for start, end, brackets in spans:
        for i in range(start, end):
            if chars[i] in brackets:
                chars[i] = chars[i].translate(_DEFUSED)
    return "".join(chars)


# 64+ символов алфавита base64 без пробелов, строки MIME-переноса склеиваются в одну серию. Слаг ссылки
# и hex-хэш — в одном регистре, поэтому условие «есть заглавная, строчная и цифра» их не трогает. Ссылки
# не исключаем: «https://x/» перед закодированной инструкцией не должно её спасать, а настоящие токены
# в ссылках (Диск, облака) короче порога.
_B64_RE = re.compile(r"[A-Za-z0-9+/_-]{64,}={0,2}(?:[ \t]*\n[ \t]*[A-Za-z0-9+/_-]{16,}={0,2})*")
_B64_MARK = "[base64 опущен: {} символов]"


def _drop_base64(text: str, flags: set[Flag]) -> str:
    def repl(m: re.Match[str]) -> str:
        run = m.group(0)
        if not (re.search(r"[A-Z]", run) and re.search(r"[a-z]", run) and re.search(r"[0-9]", run)):
            return run
        flags.add(Flag.BASE64)
        return _B64_MARK.format(sum(not ch.isspace() for ch in run))

    return _B64_RE.sub(repl, text)


COMMAND_MARK = "[!команда] "

# Начало предложения или строки: «ФНС опубликовала новые инструкции:» — новость,
# а «Новые инструкции:» с начала фразы — команда.
_SENTENCE_START = r"(?:^|(?<=[.!?…»\"]\s)|(?<=[«\"(]))"
_RU_PATTERNS = [
    # глаголы, которые в новости к читателю почти не обращены: объект любой из списка
    r"\b(?:игнорируй|забудь|отбрось|не\s+учитывай)(?:те)?\s+(?:\w+\s+){0,3}?"
    r"(?:инструкци|указани|промпт|подсказк|правил|ограничени)\w*",
    # «отмените», «пропустите» — обычные слова новостей, поэтому только с «предыдущие / свои / все»
    r"\b(?:отмени|пропусти)(?:те)?\s+(?:вс\w+\s+)?(?:предыдущ|прошл|вышеуказ|вышеизлож|прежн|сво|тво)\w*\s+"
    r"(?:\w+\s+){0,1}?(?:инструкци|указани|промпт|правил|ограничени)\w*",
    r"\b(?:ты\s+теперь|теперь\s+ты|отныне\s+ты|с\s+этого\s+момента\s+ты)\b",
    _SENTENCE_START + r"(?:нов|обновленн|настоящ|секретн|скрыт)\w*\s+инструкци\w*\s*:",
    r"\bинструкци\w*\s+для\s+(?:тебя|модели|ассистента|нейросети|ии|бота)\b",
    r"\bсистемн\w*\s+промпт\w*",
    # «покажите инструкцию по применению» — обычная фраза; команда — про свои / системные инструкции
    r"\b(?:покажи|выведи|раскрой|напечатай|повтори|перескажи)(?:те)?\s+(?:\w+\s+){0,1}?"
    r"(?:сво|тво|систем|исходн|скрыт|внутренн)\w*\s+(?:промпт|инструкци)\w*",
    r"\b(?:покажи|выведи|раскрой|напечатай)(?:те)?\s+(?:\w+\s+){0,1}?промпт\w*",
    # «поставьте оценку нашему сервису» — реклама; команда — с баллом или «максимальную»
    r"\b(?:поставь|присвой|выстави|назначь)(?:те)?\s+(?:\w+\s+){0,3}?(?:оценк|релевантност|балл)\w*\s+"
    r"(?:\w+\s+){0,2}?(?:\d|максимальн|наивысш|высш)",
    r"\b(?:поставь|присвой|выстави|назначь)(?:те)?\s+(?:\w+\s+){0,3}?(?:\d|максимальн|наивысш|высш)\w*\s+"
    r"(?:\w+\s+){0,1}?(?:оценк|релевантност|балл)",
    r"\bоцени(?:те)?\s+(?:\w+\s+){0,3}?(?:на\s+\d|в\s+\d|максимальн|наивысш|высш)",
    # «ответьте только на один вопрос» — анкета; команда — про форму ответа
    r"\b(?:отвечай|ответь)(?:те)?\s+(?:\w+\s+){0,1}?(?:только|лишь|исключительно)\s+"
    r"(?:словом|одним|«|\"|'|да\b|нет\b|ок\b|json|цифрой|числом)",
    r"\b(?:считай|признай|отметь|пометь)(?:те)?\s+(?:\w+\s+){0,2}?(?:новост|событи|публикаци|текст)\w*\s+"
    r"(?:\w+\s+){0,1}?(?:важн|релевантн|делов|бизнес)\w*",
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:системн\w*\s+сообщени\w*|ассистент)[ \t]*:",
    r"^[ \t]*#{1,6}[ \t]*(?:системн\w*|инструкци\w*)\b",
]
_EN_QUALIFIER = r"(?:all|previous|prior|above|earlier|system|your|these|any)"
_EN_PATTERNS = [
    r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:\w+\s+){0,3}?(?:instructions?|prompts?)\b",
    rf"\b(?:ignore|disregard|forget|bypass)\s+(?:the\s+)?{_EN_QUALIFIER}\s+(?:\w+\s+){{0,1}}?"
    r"(?:rules|guidelines|directions|context)\b",
    r"\byou\s+are\s+now\s+(?:an?\b|the\b|my\b|in\b|no\s+longer|free|dan\b|unrestricted|jailbroken)"
    r"|\bfrom\s+now\s+on,?\s+you\b|\bpretend\s+(?:to\s+be|you\s+are)\b",
    r"\b(?:act|behave)\s+as\s+(?:if\s+you|an?\s+(?:ai|assistant|model|chatbot|system))\b",
    _SENTENCE_START + r"(?:new|updated|real|actual|hidden|secret)\s+instructions?\s*:",
    r"\binstructions?\s+for\s+(?:you|the\s+(?:model|assistant|ai))\b",
    r"\bsystem\s+prompt\b",
    r"\b(?:reveal|print|show|output|repeat|display)\s+(?:\w+\s+){0,2}?(?:prompt|instructions)\b",
    r"\b(?:respond|answer|reply)\s+(?:\w+\s+){0,2}?only\b",
    # голая метка «System:» — только если за ней обращение к модели, а не цифры сводки
    r"^[ \t]*system[ \t]*:[ \t]*(?:you\b|your\b|new\b|from\s+now)",
    r"\bset\s+(?:\w+\s+){0,3}?(?:relevance|score|rating)\s+(?:to|=|at)\b",
    r"\b(?:mark|classify|treat|label)\s+(?:this|the)\s+(?:news|article|item|text|story)\s+as\b",
    # «System: down 3%» встречается в сводках: без «###» помечаем только редкие в новостях метки ролей
    r"^[ \t]*#{1,6}[ \t]*(?:system|assistant|developer)[ \t]*:|^[ \t]*(?:assistant|developer)[ \t]*:",
    r"^[ \t]*#{1,6}[ \t]*(?:system|instructions?)\b",
]
_RU_RE = re.compile("|".join(f"(?:{p})" for p in _RU_PATTERNS), re.I | re.M)
_EN_RE = re.compile("|".join(f"(?:{p})" for p in _EN_PATTERNS), re.I | re.M)


def _mark_commands(text: str, flags: set[Flag]) -> str:
    starts: set[int] = set()
    for pattern, table in ((_RU_RE, _TO_CYRILLIC), (_EN_RE, _TO_LATIN)):
        for m in pattern.finditer(_skeleton(text, table)):
            start = m.start()
            while start < m.end() and text[start].isspace():
                start += 1
            starts.add(start)
    if not starts:
        return text
    flags.add(Flag.COMMAND)
    parts: list[str] = []
    prev = 0
    for start in sorted(starts):
        parts.append(text[prev:start])
        # уже помеченная фраза — второй раз не помечаем, так neutralize идемпотентна
        if text[max(0, start - len(COMMAND_MARK)) : start] != COMMAND_MARK:
            parts.append(COMMAND_MARK)
        prev = start
    parts.append(text[prev:])
    return "".join(parts)


def neutralize(text: str) -> Prepared:
    flags: set[Flag] = set()
    text = _defuse_tags(sanitize(text), flags)
    text = _drop_base64(text, flags)
    text = _mark_commands(text, flags)
    return Prepared(text, frozenset(flags))


TRUNCATED_MARK = " … [обрезано]"


# для сверки цитаты модели с сохранённым текстом источника (ML-5): пометки кода в цитату не входят
def strip_marks(text: str) -> str:
    return text.replace(COMMAND_MARK, "").replace(TRUNCATED_MARK, "").translate({0x2039: "<", 0x203A: ">"})


# --- обёртка <source> ---------------------------------------------------------------------------------

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
    opening = f"<source {' '.join(head)}>"
    block = f"{opening}\n{body}\n</source>" if body else f"{opening}\n</source>"
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
