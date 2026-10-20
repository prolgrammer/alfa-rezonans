from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import BackendDep, Session, require_csrf
from app.api.errors import error_responses
from app.api.schemas import Feed
from app.contracts import FeedFilter, FeedFilters, FeedItem, Period
from app.contracts.types import Slug

router = APIRouter(tags=["feed"], dependencies=[Depends(require_csrf)], responses=error_responses(401, 422))


@router.get("/feed", response_model=Feed, summary="Лента событий с «почему у вас»")
async def feed(
    ctx: Session,
    backend: BackendDep,
    period: Period = Period.week,
    impact: Annotated[FeedFilter, Query(alias="filter")] = FeedFilter.all,
    factor: Slug | None = None,
) -> Feed:
    return await backend.feed(ctx, period, FeedFilters(impact=impact, factor=factor))


@router.get("/highlights", response_model=list[FeedItem], summary="Три события для виджета")
async def highlights(ctx: Session, backend: BackendDep) -> list[FeedItem]:
    return await backend.highlights(ctx)
