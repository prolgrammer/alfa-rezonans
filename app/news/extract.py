import asyncio
from itertools import batched

from pydantic import BaseModel, TypeAdapter

from app.contracts import Event, EventCard, NewsFilterResult, RawItemIn
from app.fixtures import load_fixture
from app.llm import LLMUnavailable, gw
from app.text.sanitize import Flag, Source, neutralize, render_sources

_EVENTS = TypeAdapter(list[Event])

FILTER_BATCH = 10  # ML-4: пачка по 10 публикаций на вызов
# ~400–500 токенов русского текста: укладываемся в 600 токенов на публикацию из ML-4
FILTER_ITEM_LIMIT = 1500
# Фоновых слотов у шлюза три, а дедлайн вызова идёт и в очереди за слотом: без предела хвост большой
# выгрузки истёк бы, не дойдя до модели, и занял бы слоты карточек событий.
FILTER_CONCURRENCY = 3
_REASON_MAX = 200
# Попытка закрыть <source> или подсунуть разметку ролей в обычной новости не встречается — такую публикацию
# не показываем ни фильтру, ни следующим шагам. Фраза-команда бывает и в настоящей новости («инструкции
# для ИИ-сервисов банков»): она уходит модели помеченной. base64 бывает во вложениях пресс-релизов.
_QUARANTINE_FLAGS = frozenset({Flag.SOURCE_TAG, Flag.MARKUP_TAG})

NO_LLM_REASON = "фильтр без LLM: пропущено дальше"
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
            pass
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
    batches = list(batched(clean, FILTER_BATCH)) + [(entry,) for entry in commanded]
    slots = asyncio.Semaphore(FILTER_CONCURRENCY)

    async def run(batch: tuple[tuple[int, RawItemIn], ...]) -> list[NewsFilterResult]:
        async with slots:
            return await _filter_batch([item for _, item in batch])

    answers = await asyncio.gather(*(run(batch) for batch in batches))
    for batch, answer in zip(batches, answers, strict=True):
        for (index, _), result in zip(batch, answer, strict=True):
            results[index] = result
    return [results[index] for index in range(len(items))]


# заглушка до ML-5
async def extract_event_card(event_id: int) -> EventCard | None:
    for event in load_fixture("events.json", _EVENTS):
        if event.id == event_id:
            return event.card
    return None
