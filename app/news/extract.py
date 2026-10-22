import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import batched

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ValidationError

from app.catalogs import factor_ids, factor_names, sector_ids, sector_names
from app.contracts import (
    CardCause,
    CardClaim,
    CardDate,
    CardFact,
    CardFactor,
    CardNumber,
    CardSector,
    EventCard,
    NewsFilterResult,
    RawItemIn,
    SourceFragment,
)
from app.contracts.enums import (
    CardStatus,
    Certainty,
    EventType,
    FactorDirection,
    Horizon,
    Magnitude,
    SectorRole,
)
from app.db import get_pool
from app.errors import NotFound
from app.llm import LLMUnavailable, Scope, current_scope, gw, llm_scope
from app.news.search import get_event_sources
from app.text.sanitize import Flag, Source, Wrapped, neutralize, render_sources, sanitize, wrap_source
from app.text.verify import QUOTE_MIN, date_mentioned, find_quote, numbers, quote_size, show_number

log = logging.getLogger(__name__)

FILTER_BATCH = 10  # ML-4: пачка по 10 публикаций на вызов
# ~400–500 токенов русского текста: укладываемся в 600 токенов на публикацию из ML-4
FILTER_ITEM_LIMIT = 1500
# Фоновых слотов у шлюза три, а дедлайн вызова идёт и в очереди за слотом: без предела хвост большой
# выгрузки истёк бы, не дойдя до модели. Два, а не три — один слот остаётся карточкам событий.
FILTER_CONCURRENCY = 2
# Публикации с фразой-командой идут по одной, но не больше стольких за вызов: лента из одних таких
# публикаций иначе стоила бы вдесятеро. Остальные пропускаются дальше без модели, как при её сбое:
# вместе в одном промпте они могли бы влиять друг на друга.
FILTER_ISOLATED_MAX = 10
_REASON_MAX = 200
# Попытка закрыть <source> или подсунуть разметку ролей в обычной новости не встречается — такую публикацию
# не показываем ни фильтру, ни следующим шагам. Фраза-команда бывает и в настоящей новости («инструкции
# для ИИ-сервисов банков»): она уходит модели помеченной. base64 бывает во вложениях пресс-релизов.
_QUARANTINE_FLAGS = frozenset({Flag.SOURCE_TAG, Flag.MARKUP_TAG})

NO_LLM_REASON = "фильтр без LLM: пропущено дальше"
OVER_ISOLATED_REASON = "фраза-команда, лимит отдельных проверок исчерпан: пропущено дальше"
NO_ANSWER_REASON = "модель не дала ответа: пропущено дальше"
EMPTY_REASON = "без пояснения модели"


class _Verdict(BaseModel):
    n: int
    business: bool
    reason: str


class NewsFilterBatch(BaseModel):
    items: list[_Verdict]


def _item_text(item: RawItemIn) -> str:
    return f"{item.title}\n{item.summary}" if item.summary else item.title


def _result(item: RawItemIn, business: bool, reason: str) -> NewsFilterResult:
    reason = reason.strip() or EMPTY_REASON
    return NewsFilterResult(raw_item_id=item.id, business=business, reason=reason[:_REASON_MAX])


def _accept(answer: NewsFilterBatch, count: int) -> dict[int, _Verdict]:
    verdicts: dict[int, _Verdict] = {}
    for verdict in answer.items:
        # номер вне пачки и повтор отбрасываем: побеждает первый ответ на номер
        if 1 <= verdict.n <= count and verdict.n not in verdicts:
            verdicts[verdict.n] = verdict
    return verdicts


async def _ask(batch: list[RawItemIn], *, retry: bool) -> dict[int, _Verdict]:
    sources = render_sources(
        [Source(_item_text(item), {"source": item.source_id}) for item in batch], limit=FILTER_ITEM_LIMIT
    )
    answer = await gw.json(
        "news_filter",
        "news_filter",
        {"sources": sources.text, "count": len(batch), "retry": retry},
        NewsFilterBatch,
        lane="background",
    )
    return _accept(answer, len(batch))


async def _filter_batch(batch: list[RawItemIn]) -> list[NewsFilterResult]:
    try:
        verdicts = await _ask(batch, retry=False)
    except LLMUnavailable:
        return [_result(item, True, NO_LLM_REASON) for item in batch]
    missing = [n for n in range(1, len(batch) + 1) if n not in verdicts]
    if missing:
        # одна перегенерация — только для публикаций без ответа; её сбой не отменяет полученные ответы
        try:
            retry = await _ask([batch[n - 1] for n in missing], retry=True)
            verdicts |= {missing[n - 1]: v for n, v in retry.items()}
        except LLMUnavailable:
            return [
                _result(item, v.business, v.reason)
                if (v := verdicts.get(n))
                else _result(item, True, NO_LLM_REASON)
                for n, item in enumerate(batch, 1)
            ]
    return [
        _result(item, v.business, v.reason)
        if (v := verdicts.get(n))
        else _result(item, True, NO_ANSWER_REASON)
        for n, item in enumerate(batch, 1)
    ]


async def news_filter(items: list[RawItemIn]) -> list[NewsFilterResult]:
    results: dict[int, NewsFilterResult] = {}
    clean: list[tuple[int, RawItemIn]] = []
    commanded: list[tuple[int, RawItemIn]] = []
    for index, item in enumerate(items):
        flags = neutralize(_item_text(item)).flags
        if suspicious := flags & _QUARANTINE_FLAGS:
            reason = "подозрение на инъекцию: " + ", ".join(sorted(suspicious))
            results[index] = _result(item, False, reason)
        elif Flag.COMMAND in flags:
            commanded.append((index, item))
        else:
            clean.append((index, item))
    # публикация с фразой-командой идёт отдельным вызовом: в общей пачке она могла бы повлиять
    # на ответы по соседним новостям, а одна — только на свой
    for index, item in commanded[FILTER_ISOLATED_MAX:]:
        results[index] = _result(item, True, OVER_ISOLATED_REASON)
    batches = list(batched(clean, FILTER_BATCH)) + [(entry,) for entry in commanded[:FILTER_ISOLATED_MAX]]
    slots = asyncio.Semaphore(FILTER_CONCURRENCY)

    async def run(batch: tuple[tuple[int, RawItemIn], ...]) -> list[NewsFilterResult]:
        async with slots:
            return await _filter_batch([item for _, item in batch])

    answers = await asyncio.gather(*(run(batch) for batch in batches))
    for batch, answer in zip(batches, answers, strict=True):
        for (index, _), result in zip(batch, answer, strict=True):
            results[index] = result
    return [results[index] for index in range(len(items))]


# --- карточка события (ML-5) ----------------------------------------------------------------------------

CARD_SOURCES = 5  # ML-5: до 5 публикаций; cites в контракте — от 1 до 5
CARD_SOURCE_LIMIT = 1500
CARD_PROMPT = "event_card"
# сколько ошибок и сколько текста элемента показать модели при перегенерации
CARD_ERRORS_MAX = 15
_ERROR_TEXT = 60


class _DraftFact(BaseModel):
    text: str
    cites: list[int]
    quote: str


class _DraftClaim(BaseModel):
    text: str
    who: str
    cites: list[int]
    quote: str


class _DraftDate(BaseModel):
    what: str
    date: str


class _DraftNumber(BaseModel):
    what: str
    value: float
    unit: str
    quote: str


class _DraftCause(BaseModel):
    text: str
    cites: list[int]


class _DraftFactor(BaseModel):
    id: str
    direction: FactorDirection
    magnitude: Magnitude
    quote: str


class _DraftSector(BaseModel):
    id: str
    role: SectorRole


class CardDraft(BaseModel):
    """Ответ модели: поля EventCard без ограничений длины, id каталогов — строкой.

    Длины и id проверяет код: иначе одна длинная строка или неверный id роняли бы весь ответ,
    а не один элемент. Закрытые списки — перечислениями: их держит строгая схема ответа.
    """

    summary: str
    facts: list[_DraftFact]
    claims: list[_DraftClaim]
    actors: list[str]
    dates: list[_DraftDate]
    numbers: list[_DraftNumber]
    causes: list[_DraftCause]
    event_type: EventType
    factors: list[_DraftFactor]
    sectors: list[_DraftSector]
    regions: list[str]
    horizon: Horizon
    certainty: Certainty


@dataclass(frozen=True)
class CardHints:
    factors: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    # сильные сигналы: факторы, которые словари нашли в заголовке события
    strong: frozenset[str] = frozenset()


@dataclass(frozen=True)
class BuiltCard:
    card: EventCard
    model: str
    retried: bool
    # что проверка удалила или заменила в принятом ответе
    removed: tuple[str, ...]


# заглушка до B2 роли 3: факторы, которые тегер словарей нашёл в заголовке события
async def title_factors(title: str) -> frozenset[str]:
    return frozenset()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?", "…")) else text + "."


def _texts(items: list[str], limit: int, count: int) -> list[str]:
    cleaned = (_clip(item, limit) for item in items)
    return list(dict.fromkeys(item for item in cleaned if item))[:count]


@dataclass
class _Checked:
    summary_ok: bool = True
    facts: list[CardFact] = field(default_factory=list)
    claims: list[CardClaim] = field(default_factory=list)
    dates: list[CardDate] = field(default_factory=list)
    numbers: list[CardNumber] = field(default_factory=list)
    causes: list[CardCause] = field(default_factory=list)
    factors: list[CardFactor] = field(default_factory=list)
    sectors: list[CardSector] = field(default_factory=list)
    # факторы из каталога, у которых не нашлась цитата: сильный сигнал из них спасается
    unquoted: list[_DraftFactor] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _Checker:
    def __init__(self, shown: Mapping[int, str]) -> None:
        self.shown = shown
        self.numbers = {n: numbers(text) for n, text in shown.items()}
        self.all_numbers: set[Decimal] = set().union(*self.numbers.values())
        self.out = _Checked()

    def fail(self, label: str, text: str, problem: str) -> None:
        self.out.errors.append(f"{label} «{_clip(text, _ERROR_TEXT)}»: {problem}")

    def quoted(
        self, label: str, text: str, cites: list[int], quote: str, *, min_size: int = QUOTE_MIN
    ) -> list[int] | None:
        """Номера источников элемента или None, если цитаты нет.

        Номер вне показанных отбрасывается; источник, где цитата нашлась, добавляется: где она стоит,
        код знает точнее модели.
        """
        if quote_size(quote) < min_size:
            self.fail(label, text, f"цитата короче {min_size} символов: приведи фразу из источника целиком")
            return None
        found = find_quote(quote, self.shown, min_size=min_size)
        if not found:
            self.fail(label, text, f"цитаты «{_clip(quote, _ERROR_TEXT)}» нет дословно ни в одном источнике")
            return None
        valid = [n for n in dict.fromkeys(cites) if n in self.shown]
        return valid + sorted(found - set(valid))

    def numbers_ok(self, label: str, text: str, cites: list[int] | None) -> bool:
        allowed = self.all_numbers if cites is None else set().union(*(self.numbers[n] for n in cites))
        missing = numbers(text) - allowed
        if missing:
            where = "в источниках" if cites is None else f"в источниках {', '.join(map(str, cites))}"
            self.fail(label, text, f"чисел {', '.join(sorted(map(show_number, missing)))} нет {where}")
            return False
        return True


def _check(draft: CardDraft, shown: Mapping[int, str]) -> _Checked:
    c = _Checker(shown)
    out = c.out
    if not draft.summary.strip():
        out.summary_ok = False
        out.errors.append("summary: пустая сводка")
    else:
        out.summary_ok = c.numbers_ok("summary", draft.summary, None)
    for i, fact in enumerate(draft.facts):
        label = f"facts[{i}]"
        cites = c.quoted(label, fact.text, fact.cites, fact.quote)
        if cites is not None and c.numbers_ok(label, fact.text, cites):
            out.facts.append(CardFact(text=_clip(fact.text, 400), cites=cites, quote=_clip(fact.quote, 500)))
    for i, claim in enumerate(draft.claims):
        label = f"claims[{i}]"
        if not claim.who.strip():
            c.fail(label, claim.text, "не сказано, чьё это мнение (who)")
            continue
        cites = c.quoted(label, claim.text, claim.cites, claim.quote)
        if cites is not None and c.numbers_ok(label, claim.text, cites):
            out.claims.append(
                CardClaim(
                    text=_clip(claim.text, 400),
                    who=_clip(claim.who, 120),
                    cites=cites,
                    quote=_clip(claim.quote, 500),
                )
            )
    for i, number in enumerate(draft.numbers):
        label = f"numbers[{i}]"
        # у числа цитата может быть короткой («11,92 ₽»): значение всё равно сверяется с ней
        if c.quoted(label, number.what, [], number.quote, min_size=1) is None:
            continue
        value = abs(Decimal(repr(number.value)))
        # и в источнике отдельным числом: короткая цитата «4» нашлась бы и внутри «14»
        if value not in numbers(number.quote) or value not in c.all_numbers:
            c.fail(label, number.what, f"значения {show_number(value)} нет в цитате")
            continue
        out.numbers.append(
            CardNumber(
                what=_clip(number.what, 120),
                value=number.value,
                unit=_clip(number.unit, 20),
                quote=_clip(number.quote, 500),
            )
        )
    for i, cause in enumerate(draft.causes):
        label = f"causes[{i}]"
        cites = [n for n in dict.fromkeys(cause.cites) if n in shown]
        if not cites:
            c.fail(label, cause.text, "нет номера показанного источника (cites)")
        elif c.numbers_ok(label, cause.text, cites):
            out.causes.append(CardCause(text=_clip(cause.text, 400), cites=cites))
    for i, item in enumerate(draft.dates):
        label = f"dates[{i}]"
        try:
            date = dt.date.fromisoformat(item.date)
        except ValueError:
            c.fail(label, item.what, f"дата «{_clip(item.date, 20)}» не в виде ГГГГ-ММ-ДД")
            continue
        if not any(date_mentioned(date, text) for text in shown.values()):
            c.fail(label, item.what, f"дня и месяца даты {date.isoformat()} нет в источниках")
            continue
        out.dates.append(CardDate(what=_clip(item.what, 120), date=date))
    known_factors = factor_ids()
    seen: set[str] = set()
    for i, factor in enumerate(draft.factors):
        label = f"factors[{i}]"
        if factor.id not in known_factors:
            c.fail(label, factor.id, "такого id нет в каталоге факторов")
            continue
        if factor.id in seen:
            continue
        seen.add(factor.id)
        if c.quoted(label, factor.id, [], factor.quote) is None:
            out.unquoted.append(factor)
            continue
        out.factors.append(
            CardFactor(
                id=factor.id,
                direction=factor.direction,
                magnitude=factor.magnitude,
                quote=_clip(factor.quote, 500),
            )
        )
    known_sectors = sector_ids()
    seen = set()
    for i, sector in enumerate(draft.sectors):
        if sector.id not in known_sectors:
            c.fail(f"sectors[{i}]", sector.id, "такого id нет в каталоге отраслей")
        elif sector.id not in seen:
            seen.add(sector.id)
            out.sectors.append(CardSector(id=sector.id, role=sector.role))
    return out


def _strong_first(checked: _Checked, title: str, strong: frozenset[str]) -> list[CardFactor]:
    """Сильный сигнал словарей модель не перебивает: фактор из заголовка остаётся в карточке."""
    have = {f.id for f in checked.factors}
    rescued: list[CardFactor] = []
    for factor in checked.unquoted:
        if factor.id in strong and factor.id not in have:
            have.add(factor.id)
            rescued.append(
                CardFactor(
                    id=factor.id,
                    direction=factor.direction,
                    magnitude=factor.magnitude,
                    quote=_clip(title, 500),
                )
            )
    for factor_id in sorted(strong - have):
        rescued.append(
            CardFactor(
                id=factor_id,
                direction=FactorDirection.unclear,
                magnitude=Magnitude.medium,
                quote=_clip(title, 500),
            )
        )
    factors = checked.factors + rescued
    return [f for f in factors if f.id in strong] + [f for f in factors if f.id not in strong]


def _assemble(draft: CardDraft, checked: _Checked, title: str, strong: frozenset[str]) -> EventCard:
    if checked.summary_ok:
        summary = draft.summary
    else:
        summary = " ".join(_sentence(f.text) for f in checked.facts[:2]) or title
    return EventCard(
        summary=_clip(summary, 600),
        facts=checked.facts[:10],
        claims=checked.claims[:10],
        actors=_texts(draft.actors, 120, 15),
        dates=checked.dates[:10],
        numbers=checked.numbers[:15],
        causes=checked.causes[:5],
        event_type=draft.event_type,
        factors=_strong_first(checked, title, strong)[:8],
        sectors=checked.sectors[:10],
        regions=_texts(draft.regions, 60, 10),
        horizon=draft.horizon,
        certainty=draft.certainty,
    )


def _source_attrs(fragment: SourceFragment) -> dict[str, str]:
    attrs = {"source": fragment.source_name, "date": fragment.published_at.date().isoformat()}
    if fragment.is_official:
        attrs["official"] = "да"
    return attrs


def _shown_text(block: Wrapped) -> str:
    # текст источника в том виде, какой видела модель: между строкой <source …> и </source>
    _, _, rest = block.text.partition("\n")
    return rest.removesuffix("</source>").removesuffix("\n")


@contextlib.contextmanager
def _card_scope(event_id: int | None) -> Iterator[Scope]:
    # своя область — только если вызывающий не открыл свою: иначе расход ушёл бы мимо его лимита
    scope = current_scope()
    if scope is not None:
        yield scope
        return
    with llm_scope(event_id=event_id) as scope:
        yield scope


def _answer_model(scope: Scope, start: int) -> str | None:
    return next((c.model for c in reversed(scope.calls[start:]) if c.task == "event_card"), None)


async def build_event_card(
    title: str, hints: CardHints, sources: Sequence[SourceFragment], *, event_id: int | None = None
) -> BuiltCard | None:
    """Карточка по публикациям события без БД. None — публикаций нет; сбой LLM поднимается наружу."""
    picked: dict[int, SourceFragment] = {}
    for fragment in sorted(sources, key=lambda s: s.n):
        if fragment.n <= CARD_SOURCES and fragment.text.strip():
            picked.setdefault(fragment.n, fragment)
    if not picked:
        return None
    blocks = [
        wrap_source(n, fragment.text, limit=CARD_SOURCE_LIMIT, **_source_attrs(fragment))
        for n, fragment in picked.items()
    ]
    shown = {int(block.n): _shown_text(block) for block in blocks}
    title = sanitize(title)
    factors, sectors = factor_names(), sector_names()
    strong = frozenset(f for f in hints.strong if f in factors)
    variables = {
        "title": neutralize(title).text,
        "count": len(blocks),
        "sources": "\n\n".join(block.text for block in blocks),
        "factors_catalog": list(factors.items()),
        "sectors_catalog": list(sectors.items()),
        "hint_factors": [(f, factors[f]) for f in dict.fromkeys(hints.factors) if f in factors],
        "hint_sectors": [(s, sectors[s]) for s in dict.fromkeys(hints.sectors) if s in sectors],
        "strong_factors": [(f, factors[f]) for f in sorted(strong)],
        "errors": [],
    }
    with _card_scope(event_id) as scope:
        start = len(scope.calls)
        draft = await gw.json("event_card", CARD_PROMPT, variables, CardDraft, event_id=event_id)
        model = _answer_model(scope, start)
        checked = _check(draft, shown)
        retried = bool(checked.errors)
        if retried:
            # одна перегенерация со списком ошибок; что не прошло и во второй раз, удаляется
            try:
                start = len(scope.calls)
                # в ошибках — куски ответа модели, а он мог повторить текст источника: обезвреживаем так же
                errors = [neutralize(e).text for e in checked.errors[:CARD_ERRORS_MAX]]
                second = await gw.json(
                    "event_card",
                    CARD_PROMPT,
                    variables | {"errors": errors},
                    CardDraft,
                    event_id=event_id,
                )
            except LLMUnavailable as error:
                log.info(
                    "карточка события %s: перегенерация не удалась, берём первый ответ: %s", event_id, error
                )
            else:
                draft, checked, model = second, _check(second, shown), _answer_model(scope, start)
    card = _assemble(draft, checked, title, strong)
    model = model or gw.config.task("event_card").model or ""
    return BuiltCard(card=card, model=model, retried=retried, removed=tuple(checked.errors))


_card_locks: dict[int, asyncio.Lock] = {}


def _stored_card(event_id: int, raw: object) -> EventCard | None:
    if raw is None:
        return None
    try:
        return EventCard.model_validate(raw)
    except ValidationError:
        log.warning("карточка события %s не проходит текущий контракт: строим заново", event_id)
        return None


def _card_key(version: str, model: str, source_count: int) -> str:
    return f"{version}|{model}|{source_count}"


def _key_matches(key: str | None, version: str, source_count: int) -> bool:
    if not key:
        return False
    parts = key.split("|")
    cfg = gw.config.task("event_card")
    return (
        len(parts) == 3
        and parts[0] == version
        and parts[1] in {cfg.model, *cfg.fallback}
        and parts[2] == str(source_count)
    )


async def _save_card(event_id: int, card: EventCard | None, status: CardStatus, key: str | None) -> None:
    payload = None if card is None else Jsonb(card.model_dump(mode="json"))
    async with (await get_pool()).connection() as conn:
        await conn.execute(
            "UPDATE events SET card = %s, card_status = %s, card_key = %s WHERE id = %s",
            (payload, status.value, key, event_id),
        )


async def extract_event_card(event_id: int) -> EventCard | None:
    # одно событие одновременно строится один раз: второй вызов дождётся первого и возьмёт его кэш
    async with _card_locks.setdefault(event_id, asyncio.Lock()):
        return await _extract_event_card(event_id)


async def _extract_event_card(event_id: int) -> EventCard | None:
    async with (await get_pool()).connection() as conn:
        cursor = await conn.execute(
            "SELECT title, source_count, factors, sectors, card, card_key FROM events WHERE id = %s",
            (event_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    previous = _stored_card(event_id, row["card"])
    version = gw.prompt_version(CARD_PROMPT)
    if previous is not None and _key_matches(row["card_key"], version, row["source_count"]):
        return previous
    if previous is None:
        await _save_card(event_id, None, CardStatus.pending, None)
    try:
        try:
            sources = await get_event_sources(event_id)
        except NotFound:
            sources = []
        hints = CardHints(
            factors=tuple(row["factors"]),
            sectors=tuple(row["sectors"]),
            strong=await title_factors(row["title"]),
        )
        built = await build_event_card(row["title"], hints, sources, event_id=event_id)
    except LLMUnavailable as error:
        log.warning("карточка события %s: модель недоступна (%s)", event_id, error)
        built = None
    except Exception:
        # ошибка кода, а не модели: статус не должен навсегда остаться pending
        if previous is None:
            await _save_card(event_id, None, CardStatus.failed, None)
        raise
    if built is None:
        if previous is None:
            await _save_card(event_id, None, CardStatus.failed, None)
        # прежняя карточка остаётся с прежним ключом: следующий проход конвейера попробует снова
        return previous
    key = _card_key(version, built.model, row["source_count"])
    await _save_card(event_id, built.card, CardStatus.ok, key)
    return built.card
