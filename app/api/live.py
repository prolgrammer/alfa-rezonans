from collections.abc import AsyncIterator
from typing import NoReturn

from app.agent.run import run_chat
from app.api.errors import ApiError
from app.api.schemas import (
    LOW_DATA_COMPLETENESS,
    ChatRequest,
    Compare,
    EventDetail,
    Feed,
    FeedbackIn,
    InsightStart,
    LlmUsage,
    NewsCheck,
    NewsCheckResult,
    Profile,
    ProfilePatch,
    ProfileText,
    ProfileTextPreview,
    Quality,
    ReviewIn,
    ReviewQueueItem,
    SourceHealth,
    ToolCallLog,
    Trace,
    Why,
)
from app.api.sse import SseEvent, agent_events
from app.clients.counterparties import get_counterparties
from app.clients.profile import get_profile
from app.collect.market import get_market_series
from app.contracts import (
    CounterpartyDirection,
    FeedFilters,
    FeedItem,
    MarketSeries,
    Period,
    SessionCtx,
    ThesisItem,
)
from app.rank.feed import explain, highlights, rank_feed


def not_implemented(item: str) -> NoReturn:
    raise ApiError(503, "not_implemented", f"Ещё не реализовано ({item})")


class LiveBackend:
    async def company_name(self, ctx: SessionCtx) -> str:
        not_implemented("BE-9")

    async def has_company(self, company: str) -> bool:
        not_implemented("BE-9")

    async def feed(self, ctx: SessionCtx, period: Period, filters: FeedFilters) -> Feed:
        profile = await get_profile(ctx.company_id)
        items = await rank_feed(ctx, period, filters)
        return Feed(
            items=items,
            completeness=profile.completeness,
            low_data=profile.completeness < LOW_DATA_COMPLETENESS,
        )

    async def highlights(self, ctx: SessionCtx) -> list[FeedItem]:
        return await highlights(ctx)

    async def event_detail(self, ctx: SessionCtx, event_id: int) -> EventDetail:
        not_implemented("BE-8")

    async def why(self, ctx: SessionCtx, event_id: int) -> Why:
        score = await explain(ctx, event_id)
        items = await rank_feed(ctx, Period.month, FeedFilters())
        chips = next((i.why for i in items if i.event_id == event_id), [])
        return Why(score=score, chips=chips)

    async def start_insight(self, ctx: SessionCtx, event_id: int) -> InsightStart:
        not_implemented("BE-8")

    async def stream_insight(self, ctx: SessionCtx, insight_id: int, after: int) -> AsyncIterator[SseEvent]:
        not_implemented("BE-8")

    async def trace(self, ctx: SessionCtx, insight_id: int) -> Trace:
        not_implemented("BE-8")

    async def chat(self, ctx: SessionCtx, body: ChatRequest, after: int) -> AsyncIterator[SseEvent]:
        # история чата хранится в БД (BE-8), пока вопрос идёт без неё
        return agent_events(run_chat(ctx, body.event_id, [], body.question))

    async def check_news(self, ctx: SessionCtx, body: NewsCheck) -> NewsCheckResult:
        not_implemented("BE-8a")

    async def profile(self, ctx: SessionCtx) -> Profile:
        return Profile(
            profile=await get_profile(ctx.company_id),
            suppliers=await get_counterparties(ctx, CounterpartyDirection.suppliers),
            customers=await get_counterparties(ctx, CounterpartyDirection.customers),
        )

    async def patch_profile(self, ctx: SessionCtx, body: ProfilePatch) -> Profile:
        not_implemented("DQ-4")

    async def profile_text(self, ctx: SessionCtx, body: ProfileText) -> ProfileTextPreview:
        not_implemented("DQ-4")

    async def compare(self, ctx: SessionCtx, event_id: int, a: str, b: str) -> Compare:
        not_implemented("BE-8")

    async def market(self, series: str, days: int) -> MarketSeries:
        return await get_market_series(series, days)

    async def bank_page(self, doc_id: int, page: int) -> bytes:
        not_implemented("BE-7")

    async def feedback(self, ctx: SessionCtx, body: FeedbackIn) -> None:
        not_implemented("BE-8")

    async def expert_review(self, ctx: SessionCtx, body: ReviewIn) -> None:
        not_implemented("BE-8")

    async def review_queue(self) -> list[ReviewQueueItem]:
        not_implemented("BE-8")

    async def quality(self) -> Quality:
        not_implemented("DQ-14")

    async def llm_usage(self) -> LlmUsage:
        not_implemented("ML-3")

    async def tool_calls(self) -> list[ToolCallLog]:
        not_implemented("ML-10")

    async def sources_health(self) -> list[SourceHealth]:
        not_implemented("BE-4")

    async def theses(self) -> list[ThesisItem]:
        not_implemented("ML-6")

    async def verify_thesis(self, thesis_id: int, verified: bool) -> ThesisItem:
        not_implemented("ML-6")
