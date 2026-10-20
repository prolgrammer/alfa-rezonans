from pydantic import TypeAdapter

from app.contracts import (
    Access,
    FeedFilter,
    FeedFilters,
    FeedItem,
    Period,
    RelevanceScore,
    SessionCtx,
    UserRole,
)
from app.fixtures import load_fixture

_FEED = TypeAdapter(dict[str, list[FeedItem]])
_RELEVANCE = TypeAdapter(dict[str, list[RelevanceScore]])

HIGHLIGHTS = 3


def _feed_of(company_id: str) -> list[FeedItem]:
    feed = load_fixture("feed.json", _FEED)
    if company_id not in feed:
        raise LookupError(f"лента компании {company_id} не найдена")
    return sorted(feed[company_id], key=lambda i: -i.score)


# заглушка до DQ-9
async def rank_feed(ctx: SessionCtx, period: Period, filters: FeedFilters) -> list[FeedItem]:
    # Фикстурная лента короче любого периода: с его учётом week и day дали бы пустой экран.
    items = _feed_of(ctx.company_id)
    if filters.impact != FeedFilter.all:
        items = [i for i in items if i.impact and i.impact.direction.value == filters.impact.value]
    if filters.factor is not None:
        items = [i for i in items if filters.factor in i.factors]
    return items


# заглушка до DQ-10
async def explain(ctx: SessionCtx, event_id: int) -> RelevanceScore:
    rows = load_fixture("relevance.json", _RELEVANCE).get(ctx.company_id, [])
    for row in rows:
        if row.event_id == event_id:
            return row
    raise LookupError(f"оценки события {event_id} для компании {ctx.company_id} нет")


# заглушка до DQ-11
async def highlights(ctx: SessionCtx) -> list[FeedItem]:
    picked: list[FeedItem] = []
    seen: set[str | None] = set()
    for item in _feed_of(ctx.company_id):
        if item.main_factor in seen:
            continue
        seen.add(item.main_factor)
        picked.append(item)
        if len(picked) == HIGHLIGHTS:
            break
    return picked


# заглушка до DQ-11
async def warm_list() -> list[tuple[SessionCtx, int]]:
    # user_id 0 — служебный: прогрев идёт без сессии человека
    return [
        (
            SessionCtx(user_id=0, company_id=company_id, user_role=UserRole.cfo, access=Access.client),
            i.event_id,
        )
        for company_id, items in load_fixture("feed.json", _FEED).items()
        for i in items
    ]
