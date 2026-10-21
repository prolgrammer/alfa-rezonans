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
_REASON_MAX = 200
# base64 бывает и во вложениях настоящих пресс-релизов, поэтому сам по себе не повод отсеять
_INJECTION_FLAGS = frozenset({Flag.SOURCE_TAG, Flag.MARKUP_TAG, Flag.COMMAND})

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


def _accept(answer: NewsFilterBatch, count: int) -> tuple[dict[int, _Verdict], list[str]]:
    verdicts: dict[int, _Verdict] = {}
    errors: list[str] = []
    for verdict in answer.items:
        if not 1 <= verdict.n <= count:
            errors.append(f"номер {verdict.n} вне 1..{count}")
        elif verdict.n in verdicts:
            errors.append(f"номер {verdict.n} повторяется")
        else:
            verdicts[verdict.n] = verdict
    missing = [n for n in range(1, count + 1) if n not in verdicts]
    if missing:
        errors.append(f"нет ответа для номеров {', '.join(map(str, missing))}")
    return verdicts, errors


async def _ask(batch: list[RawItemIn], errors: list[str]) -> tuple[dict[int, _Verdict], list[str]]:
    sources = render_sources(
        [Source(_item_text(item), {"source": item.source_id}) for item in batch], limit=FILTER_ITEM_LIMIT
    )
    answer = await gw.json(
        "news_filter",
        "news_filter",
        {"sources": sources.text, "count": len(batch), "errors": errors},
        NewsFilterBatch,
        lane="background",
    )
    return _accept(answer, len(batch))


async def _filter_batch(batch: list[RawItemIn]) -> list[NewsFilterResult]:
    try:
        verdicts, errors = await _ask(batch, [])
        missing = [n for n in range(1, len(batch) + 1) if n not in verdicts]
        if missing:
            # одна перегенерация со списком ошибок — только для публикаций без ответа
            retry, _ = await _ask([batch[n - 1] for n in missing], errors)
            verdicts |= {missing[n - 1]: v for n, v in retry.items()}
    except LLMUnavailable:
        return [_result(item, True, NO_LLM_REASON) for item in batch]
    return [
        _result(item, v.business, v.reason)
        if (v := verdicts.get(n))
        else _result(item, True, NO_ANSWER_REASON)
        for n, item in enumerate(batch, 1)
    ]


async def news_filter(items: list[RawItemIn]) -> list[NewsFilterResult]:
    results: dict[int, NewsFilterResult] = {}
    to_model: list[tuple[int, RawItemIn]] = []
    for index, item in enumerate(items):
        suspicious = neutralize(_item_text(item)).flags & _INJECTION_FLAGS
        if suspicious:
            # текст, который пытается управлять моделью, не показываем ни фильтру, ни следующим шагам
            reason = "подозрение на инъекцию: " + ", ".join(sorted(suspicious))
            results[index] = _result(item, False, reason)
        else:
            to_model.append((index, item))
    batches = list(batched(to_model, FILTER_BATCH))
    answers = await asyncio.gather(*(_filter_batch([item for _, item in batch]) for batch in batches))
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
