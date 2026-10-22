import asyncio
import random
from collections.abc import AsyncIterator

from pydantic import TypeAdapter

from app.agent.brief import BankViewFn, run_brief
from app.agent.trace import TraceSink
from app.bank.theses import get_bank_view
from app.contracts import (
    AgentStep,
    ChatAnswer,
    ChatTurn,
    Event,
    InsightResult,
    SessionCtx,
    StepKind,
)
from app.errors import NotFound
from app.fixtures import load_fixture
from app.llm import LLMUnavailable, gw

# Пауза между шагами имитирует работу агента, чтобы интерфейс видел живой поток. Тесты ставят (0, 0).
STEP_DELAY_S = (0.3, 0.8)

_EVENTS = TypeAdapter(list[Event])
_TRACE = TypeAdapter(list[AgentStep])
_INSIGHT = TypeAdapter(InsightResult)
_CHAT_ANSWER = TypeAdapter(ChatAnswer)

_CHAT_KINDS = (StepKind.plan, StepKind.tool, StepKind.final)

# Позиция банка для краткого разбора; тесты без БД подменяют её на fixture_bank_view.
_bank_view: BankViewFn = get_bank_view


def _require_event(event_id: int) -> None:
    _load_event(event_id)


async def _pause() -> None:
    await asyncio.sleep(random.uniform(*STEP_DELAY_S))


# заглушка до BE-8 роли 3
def _load_event(event_id: int) -> Event:
    for event in load_fixture("events.json", _EVENTS):
        if event.id == event_id:
            return event
    raise NotFound(f"событие {event_id} не найдено")


# заглушка до ML-13
async def _run_full(ctx: SessionCtx, event: Event) -> AsyncIterator[AgentStep | InsightResult]:
    # полный путь первым делом обращается к модели: без ключа разбор сразу уходит в краткий
    gw.check_configured()
    for step in load_fixture("trace.json", _TRACE):
        await _pause()
        yield step
    yield load_fixture("insight.json", _INSIGHT)


async def run_insight(
    ctx: SessionCtx, event_id: int, *, insight_id: int | None = None
) -> AsyncIterator[AgentStep | InsightResult]:
    event = _load_event(event_id)
    last_seq = 0
    try:
        async for item in _run_full(ctx, event):
            if isinstance(item, AgentStep):
                last_seq = item.seq
            yield item
        return
    except LLMUnavailable as error:
        failure = error
    # номера шагов краткого разбора продолжают уже отданные: интерфейс видит один сквозной поток
    sink = TraceSink(insight_id=insight_id, start=last_seq)
    async for item in run_brief(ctx, event, sink=sink, error=failure, bank_view=_bank_view):
        yield item


# заглушка до ML-15
async def run_chat(
    ctx: SessionCtx, event_id: int | None, history: list[ChatTurn], question: str
) -> AsyncIterator[AgentStep | ChatAnswer]:
    if event_id is not None:
        _require_event(event_id)
    steps = load_fixture("trace.json", _TRACE)
    for seq, kind in enumerate(_CHAT_KINDS, start=1):
        step = next(s for s in steps if s.kind == kind)
        await _pause()
        yield step.model_copy(update={"seq": seq})
    yield load_fixture("chat_answer.json", _CHAT_ANSWER)
