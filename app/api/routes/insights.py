from fastapi import APIRouter, Depends, Request

from app.api.deps import BackendDep, Session, require_csrf
from app.api.errors import error_responses
from app.api.schemas import Trace
from app.api.sse import INSIGHT_STREAM_MODELS, EventStreamResponse, last_event_id, sse_docs, sse_response

router = APIRouter(
    prefix="/insights",
    tags=["insights"],
    dependencies=[Depends(require_csrf)],
    responses=error_responses(401, 422),
)


@router.get(
    "/{insight_id}/stream",
    response_class=EventStreamResponse,
    responses={
        **sse_docs(
            INSIGHT_STREAM_MODELS,
            "Поток SSE: `step` (AgentStep) …, `insight` (InsightResult), `done`; при сбое `error` и `done`. "
            "Кадры нумеруются с 1; заголовок Last-Event-ID возобновляет поток после указанного кадра.",
        ),
        **error_responses(404, 503),
    },
    summary="Ход разбора потоком",
)
async def stream_insight(insight_id: int, request: Request, ctx: Session, backend: BackendDep):
    events = await backend.stream_insight(ctx, insight_id)
    return sse_response(events, last_event_id(request))


@router.get(
    "/{insight_id}/trace",
    response_model=Trace,
    responses=error_responses(404, 503),
    summary="Трасса агента",
)
async def trace(insight_id: int, ctx: Session, backend: BackendDep) -> Trace:
    return await backend.trace(ctx, insight_id)
