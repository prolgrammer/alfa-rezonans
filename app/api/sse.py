import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from app.api.errors import ErrorBody, error_body
from app.contracts import AgentStep, ChatAnswer, InsightResult

EventName = Literal["step", "insight", "answer", "error", "done"]


@dataclass(frozen=True)
class SseEvent:
    event: EventName
    data: BaseModel | dict


class EventStreamResponse(StreamingResponse):
    media_type = "text/event-stream"


def agent_events(stream: AsyncIterator[AgentStep | InsightResult | ChatAnswer]) -> AsyncIterator[SseEvent]:
    async def events() -> AsyncIterator[SseEvent]:
        async for item in stream:
            match item:
                case AgentStep():
                    yield SseEvent("step", item)
                case InsightResult():
                    yield SseEvent("insight", item)
                case ChatAnswer():
                    yield SseEvent("answer", item)

    return events()


def last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id", "")
    # мусор в заголовке не должен ломать переподключение: тогда поток идёт с начала
    return int(raw) if raw.isascii() and raw.isdigit() else None


def _frame(n: int, event: SseEvent) -> str:
    payload = event.data.model_dump(mode="json") if isinstance(event.data, BaseModel) else event.data
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {n}\nevent: {event.event}\ndata: {data}\n\n"


async def _with_tail(events: AsyncIterator[SseEvent]) -> AsyncIterator[SseEvent]:
    try:
        async for event in events:
            yield event
    except Exception as exc:
        # Заголовки уже отправлены, статус не поменять: ошибку отдаём кадром, чтобы клиент не завис.
        yield SseEvent("error", error_body(exc)[1])
    yield SseEvent("done", {})


async def _frames(events: AsyncIterator[SseEvent], last_event_id: int | None) -> AsyncIterator[str]:
    skip = last_event_id or 0
    n = 0
    async for event in _with_tail(events):
        n += 1
        # Пройденные кадры не шлём, но генератор прогоняем: шаги детерминированы, номера совпадут.
        if n > skip:
            yield _frame(n, event)


def sse_response(events: AsyncIterator[SseEvent], last_event_id: int | None) -> EventStreamResponse:
    return EventStreamResponse(
        _frames(events, last_event_id),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


INSIGHT_STREAM_MODELS = (AgentStep, InsightResult, ErrorBody)
CHAT_STREAM_MODELS = (AgentStep, ChatAnswer, ErrorBody)

_REF = "#/components/schemas/{model}"


def sse_docs(models: tuple[type[BaseModel], ...], description: str) -> dict[int | str, dict[str, Any]]:
    schema = {"oneOf": [{"$ref": _REF.format(model=m.__name__)} for m in models]}
    return {200: {"description": description, "content": {"text/event-stream": {"schema": schema}}}}


def sse_component_schemas() -> dict[str, Any]:
    """Схемы данных кадров: на них в потоке никто не ссылается через модели ответов, а типы фронту нужны."""
    models = {*INSIGHT_STREAM_MODELS, *CHAT_STREAM_MODELS}
    _, defs = models_json_schema([(m, "serialization") for m in models], ref_template=_REF)
    return defs["$defs"]
