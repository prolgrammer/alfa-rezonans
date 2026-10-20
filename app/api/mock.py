from collections.abc import AsyncIterator

from pydantic import JsonValue, TypeAdapter

from app.agent.run import run_chat, run_insight
from app.api.live import LiveBackend
from app.api.schemas import (
    ChatRequest,
    Compare,
    CompareSide,
    EventDetail,
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
)
from app.api.sse import SseEvent, agent_events
from app.bank.theses import get_bank_view
from app.clients.profile import get_profile
from app.collect.market import get_market_series
from app.contracts import (
    AgentStep,
    CompanyProfile,
    Event,
    Feature,
    FeedFilters,
    InsightResult,
    InsightStatus,
    Period,
    SessionCtx,
    ThesisItem,
)
from app.fixtures import load_fixture
from app.news.search import get_event_sources
from app.rank.feed import explain, rank_feed
from app.settings import get_settings

_EVENTS = TypeAdapter(list[Event])
_PROFILES = TypeAdapter(list[CompanyProfile])
_THESES = TypeAdapter(list[ThesisItem])
_TRACE = TypeAdapter(list[AgentStep])
_INSIGHT = TypeAdapter(InsightResult)
_REVIEW_QUEUE = TypeAdapter(list[ReviewQueueItem])
_QUALITY = TypeAdapter(Quality)
_LLM_USAGE = TypeAdapter(LlmUsage)
_TOOL_CALLS = TypeAdapter(list[ToolCallLog])
_SOURCES_HEALTH = TypeAdapter(list[SourceHealth])

# Названия вымышлены, как и сами компании фикстур
_COMPANY_NAMES = {"C-DEV-01": "ООО «Стройинвест» (демо)", "C-IT-01": "ООО «Нордсофт» (демо)"}
# Соответствие фактор → ряд появится в config/factors.yaml (DQ-6); пока в фикстурах два ряда
_FACTOR_SERIES = {"key_rate": "cbr_key_rate", "cny_rub": "cbr_cny"}
# Разбор в фикстуре один: событие 1 для C-DEV-01
_INSIGHT_OWNER = ("C-DEV-01", 1)
MARKET_DAYS = 90
BANK_PER_FACTOR = 3


class MockBackend(LiveBackend):
    """Всё, чего у границ ролей ещё нет, отдаёт из фикстур; изменения живут в памяти процесса."""

    def __init__(self) -> None:
        self._profile_overrides: dict[str, dict[str, JsonValue]] = {}
        self._verified: dict[int, bool] = {}

    @staticmethod
    def _event(event_id: int) -> Event:
        for event in load_fixture("events.json", _EVENTS):
            if event.id == event_id:
                return event
        raise LookupError(f"событие {event_id} не найдено")

    async def company_name(self, ctx: SessionCtx) -> str:
        return _COMPANY_NAMES.get(ctx.company_id, ctx.company_id)

    async def has_company(self, company: str) -> bool:
        return any(p.company_id == company for p in load_fixture("profiles.json", _PROFILES))

    async def event_detail(self, ctx: SessionCtx, event_id: int) -> EventDetail:
        event = self._event(event_id)
        market = [
            await get_market_series(_FACTOR_SERIES[f], MARKET_DAYS)
            for f in event.factors
            if f in _FACTOR_SERIES
        ]
        bank: dict[int, ThesisItem] = {}
        for factor in event.factors:
            for item in await get_bank_view(factor, BANK_PER_FACTOR):
                bank.setdefault(item.id, item)
        return EventDetail(
            event=event,
            sources=await get_event_sources(event_id),
            market=market,
            bank=list(bank.values()),
        )

    async def start_insight(self, ctx: SessionCtx, event_id: int) -> InsightStart:
        self._event(event_id)
        feed = await rank_feed(ctx, Period.month, FeedFilters())
        cached = any(i.event_id == event_id and i.insight_id is not None for i in feed)
        status = InsightStatus.done if cached else InsightStatus.running
        # в MOCK разбор не хранится: его номер совпадает с номером события
        return InsightStart(insight_id=event_id, status=status, cached=cached)

    async def stream_insight(self, ctx: SessionCtx, insight_id: int) -> AsyncIterator[SseEvent]:
        self._event(insight_id)
        return agent_events(run_insight(ctx, insight_id))

    async def trace(self, ctx: SessionCtx, insight_id: int) -> Trace:
        self._event(insight_id)
        return Trace(insight_id=insight_id, steps=load_fixture("trace.json", _TRACE))

    async def chat(self, ctx: SessionCtx, body: ChatRequest) -> AsyncIterator[SseEvent]:
        if body.event_id is not None:
            self._event(body.event_id)
        return agent_events(run_chat(ctx, body.event_id, [], body.question))

    async def check_news(self, ctx: SessionCtx, body: NewsCheck) -> NewsCheckResult:
        # своя новость в MOCK всегда привязывается к событию 1
        return NewsCheckResult(event_id=1, insight_id=1, relevance=await explain(ctx, 1))

    async def profile(self, ctx: SessionCtx) -> Profile:
        base = await super().profile(ctx)
        overrides = self._profile_overrides.get(ctx.company_id, {})
        if not overrides:
            return base
        features = [f for f in base.profile.features if f.key not in overrides]
        features += [
            Feature(key=key, value=value, source="user", detail="указано вами")
            for key, value in overrides.items()
        ]
        profile = base.profile.model_copy(update={"features": features, "version": base.profile.version + 1})
        return base.model_copy(update={"profile": profile})

    async def patch_profile(self, ctx: SessionCtx, body: ProfilePatch) -> Profile:
        self._profile_overrides.setdefault(ctx.company_id, {})[body.key] = body.value
        return await self.profile(ctx)

    async def profile_text(self, ctx: SessionCtx, body: ProfileText) -> ProfileTextPreview:
        # Разбор текста идёт через модель (DQ-4); в MOCK чувствительности не меняются.
        exposures = (await get_profile(ctx.company_id)).exposures
        return ProfileTextPreview(before=exposures, after=exposures)

    async def compare(self, ctx: SessionCtx, event_id: int, a: str, b: str) -> Compare:
        self._event(event_id)
        insight = load_fixture("insight.json", _INSIGHT)

        async def side(company: str) -> CompareSide:
            if not await self.has_company(company):
                raise LookupError(f"компания {company} не найдена")
            as_company = ctx.model_copy(update={"company_id": company})
            feed = await rank_feed(as_company, Period.month, FeedFilters())
            return CompareSide(
                company_id=company,
                feed=feed,
                insight=insight if (company, event_id) == _INSIGHT_OWNER else None,
            )

        return Compare(event_id=event_id, a=await side(a), b=await side(b))

    async def bank_page(self, doc_id: int, page: int) -> bytes:
        if all(t.doc_id != doc_id for t in load_fixture("theses.json", _THESES)):
            raise LookupError(f"документ {doc_id} не найден")
        return (get_settings().fixtures_dir / "bank_page.png").read_bytes()

    async def feedback(self, ctx: SessionCtx, body: FeedbackIn) -> None:
        self._event(body.event_id)

    async def expert_review(self, ctx: SessionCtx, body: ReviewIn) -> None:
        if all(r.insight_id != body.insight_id for r in load_fixture("review_queue.json", _REVIEW_QUEUE)):
            raise LookupError(f"разбора {body.insight_id} нет в очереди")

    async def review_queue(self) -> list[ReviewQueueItem]:
        return load_fixture("review_queue.json", _REVIEW_QUEUE)

    async def quality(self) -> Quality:
        return load_fixture("quality.json", _QUALITY)

    async def llm_usage(self) -> LlmUsage:
        return load_fixture("llm_usage.json", _LLM_USAGE)

    async def tool_calls(self) -> list[ToolCallLog]:
        return load_fixture("tool_calls.json", _TOOL_CALLS)

    async def sources_health(self) -> list[SourceHealth]:
        return load_fixture("sources_health.json", _SOURCES_HEALTH)

    async def theses(self) -> list[ThesisItem]:
        return [
            t.model_copy(update={"verified": self._verified.get(t.id, t.verified)})
            for t in load_fixture("theses.json", _THESES)
        ]

    async def verify_thesis(self, thesis_id: int, verified: bool) -> ThesisItem:
        if all(t.id != thesis_id for t in await self.theses()):
            raise LookupError(f"тезис {thesis_id} не найден")
        self._verified[thesis_id] = verified
        return next(t for t in await self.theses() if t.id == thesis_id)
