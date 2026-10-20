from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import BackendDep, Session, require_csrf
from app.api.errors import error_responses
from app.contracts import MarketSeries

router = APIRouter(tags=["market"], dependencies=[Depends(require_csrf)], responses=error_responses(401, 422))


@router.get(
    "/market/{series}",
    response_model=MarketSeries,
    responses=error_responses(404),
    summary="Ряд для графика",
)
async def market(
    series: str, _: Session, backend: BackendDep, days: Annotated[int, Query(ge=1, le=365)] = 90
) -> MarketSeries:
    return await backend.market(series, days)
