from fastapi import APIRouter, Depends

from app.api.deps import BackendDep, Expert, Session, require_csrf
from app.api.errors import error_responses
from app.api.schemas import FeedbackIn, Ok, ReviewIn, ReviewQueueItem

router = APIRouter(
    tags=["feedback"], dependencies=[Depends(require_csrf)], responses=error_responses(401, 422)
)


@router.post(
    "/feedback",
    response_model=Ok,
    responses=error_responses(403, 404, 503),
    summary="Отзыв: полезно или мимо",
)
async def feedback(body: FeedbackIn, ctx: Session, backend: BackendDep) -> Ok:
    await backend.feedback(ctx, body)
    return Ok(ok=True)


@router.post(
    "/expert/reviews",
    response_model=Ok,
    responses=error_responses(403, 404, 503),
    summary="Решение эксперта по разбору",
)
async def expert_review(body: ReviewIn, ctx: Expert, backend: BackendDep) -> Ok:
    await backend.expert_review(ctx, body)
    return Ok(ok=True)


@router.get(
    "/expert/reviews",
    response_model=list[ReviewQueueItem],
    responses=error_responses(403, 503),
    summary="Очередь эксперта",
)
async def review_queue(_: Expert, backend: BackendDep) -> list[ReviewQueueItem]:
    return await backend.review_queue()
