from collections.abc import AsyncIterator
from typing import Protocol

from app.api.schemas import (
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
from app.api.sse import SseEvent
from app.contracts import FeedFilters, FeedItem, MarketSeries, Period, SessionCtx, ThesisItem


class Backend(Protocol):
    """Реализация маршрутов: MockBackend читает фикстуры, LiveBackend зовёт функции границ ролей."""

    async def company_name(self, ctx: SessionCtx) -> str: ...

    async def has_company(self, company: str) -> bool: ...

    async def feed(self, ctx: SessionCtx, period: Period, filters: FeedFilters) -> Feed: ...

    async def highlights(self, ctx: SessionCtx) -> list[FeedItem]: ...

    async def event_detail(self, ctx: SessionCtx, event_id: int) -> EventDetail: ...

    async def why(self, ctx: SessionCtx, event_id: int) -> Why: ...

    async def start_insight(self, ctx: SessionCtx, event_id: int) -> InsightStart: ...

    # `after` — номер последнего кадра, который клиент уже получил: пройденное повторять быстро, без пауз.
    # Проверка существования — до начала потока: после первого кадра статус ответа уже не изменить.
    async def stream_insight(
        self, ctx: SessionCtx, insight_id: int, after: int
    ) -> AsyncIterator[SseEvent]: ...

    async def trace(self, ctx: SessionCtx, insight_id: int) -> Trace: ...

    async def chat(self, ctx: SessionCtx, body: ChatRequest, after: int) -> AsyncIterator[SseEvent]: ...

    async def check_news(self, ctx: SessionCtx, body: NewsCheck) -> NewsCheckResult: ...

    async def profile(self, ctx: SessionCtx) -> Profile: ...

    async def patch_profile(self, ctx: SessionCtx, body: ProfilePatch) -> Profile: ...

    async def profile_text(self, ctx: SessionCtx, body: ProfileText) -> ProfileTextPreview: ...

    async def compare(self, ctx: SessionCtx, event_id: int, a: str, b: str) -> Compare: ...

    async def market(self, series: str, days: int) -> MarketSeries: ...

    async def bank_page(self, doc_id: int, page: int) -> bytes: ...

    async def feedback(self, ctx: SessionCtx, body: FeedbackIn) -> None: ...

    async def expert_review(self, ctx: SessionCtx, body: ReviewIn) -> None: ...

    async def review_queue(self) -> list[ReviewQueueItem]: ...

    async def quality(self) -> Quality: ...

    async def llm_usage(self) -> LlmUsage: ...

    async def tool_calls(self) -> list[ToolCallLog]: ...

    async def sources_health(self) -> list[SourceHealth]: ...

    async def theses(self) -> list[ThesisItem]: ...

    async def verify_thesis(self, thesis_id: int, verified: bool) -> ThesisItem: ...
