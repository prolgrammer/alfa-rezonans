from fastapi import APIRouter, Depends

from app.api.deps import BackendDep, Session, require_csrf
from app.api.errors import error_responses
from app.api.schemas import EventDetail, InsightStart, Why

router = APIRouter(
    prefix="/events",
    tags=["events"],
    dependencies=[Depends(require_csrf)],
    responses=error_responses(401, 422),
)


@router.get(
    "/{event_id}",
    response_model=EventDetail,
    responses=error_responses(404, 503),
    summary="Карточка события, публикации, рынок и позиция банка",
)
async def event_detail(event_id: int, ctx: Session, backend: BackendDep) -> EventDetail:
    return await backend.event_detail(ctx, event_id)


@router.get(
    "/{event_id}/why",
    response_model=Why,
    responses=error_responses(404),
    summary="Почему событие показано или не показано",
)
async def why(event_id: int, ctx: Session, backend: BackendDep) -> Why:
    return await backend.why(ctx, event_id)


@router.post(
    "/{event_id}/insight",
    response_model=InsightStart,
    responses=error_responses(403, 404, 503),
    summary="Запросить разбор: из кэша или запуск агента",
)
async def start_insight(event_id: int, ctx: Session, backend: BackendDep) -> InsightStart:
    return await backend.start_insight(ctx, event_id)
