import asyncio
import random
from collections.abc import AsyncIterator

from pydantic import TypeAdapter

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

# Пауза между шагами имитирует работу агента, чтобы интерфейс видел живой поток. Тесты ставят (0, 0).
STEP_DELAY_S = (0.3, 0.8)

_EVENTS = TypeAdapter(list[Event])
_TRACE = TypeAdapter(list[AgentStep])
_INSIGHT = TypeAdapter(InsightResult)
_CHAT_ANSWER = TypeAdapter(ChatAnswer)

_CHAT_KINDS = (StepKind.plan, StepKind.tool, StepKind.final)


def _require_event(event_id: int) -> None:
    if all(e.id != event_id for e in load_fixture("events.json", _EVENTS)):
        raise NotFound(f"событие {event_id} не найдено")


async def _pause() -> None:
    await asyncio.sleep(random.uniform(*STEP_DELAY_S))


# заглушка до ML-13
async def run_insight(ctx: SessionCtx, event_id: int) -> AsyncIterator[AgentStep | InsightResult]:
    _require_event(event_id)
    for step in load_fixture("trace.json", _TRACE):
        await _pause()
        yield step
    yield load_fixture("insight.json", _INSIGHT)


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
