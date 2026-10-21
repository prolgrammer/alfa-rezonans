from fastapi import APIRouter, Depends

from app.api.deps import BackendDep, Session, require_csrf
from app.api.errors import error_responses
from app.api.schemas import NewsCheck, NewsCheckResult

router = APIRouter(tags=["news"], dependencies=[Depends(require_csrf)], responses=error_responses(401, 422))


@router.post(
    "/news/check",
    response_model=NewsCheckResult,
    responses=error_responses(403, 503),
    summary="Проверить свою новость; ход разбора — по /insights/{id}/stream",
)
async def check_news(body: NewsCheck, ctx: Session, backend: BackendDep) -> NewsCheckResult:
    return await backend.check_news(ctx, body)
