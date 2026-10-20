import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, AwareDatetime, Field, JsonValue, model_validator

from app.contracts import (
    Access,
    AgentStep,
    CompanyProfile,
    CounterpartyShare,
    Event,
    ExpertAction,
    Exposure,
    FeedbackReason,
    FeedbackVerdict,
    FeedItem,
    InsightResult,
    InsightStatus,
    MarketSeries,
    RelevanceScore,
    SourceFragment,
    SourceKind,
    ThesisItem,
    UserRole,
    WhyChip,
)
from app.contracts._base import ContractModel
from app.contracts.types import Slug

# Ниже этой полноты профиля интерфейс показывает плашку «мало данных» (DQ-3)
LOW_DATA_COMPLETENESS = 0.6


class Health(ContractModel):
    status: Literal["ok"]
    mock: bool


class Ok(ContractModel):
    ok: Literal[True]


class LoginRequest(ContractModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class Me(ContractModel):
    user_id: int
    login: str
    company_id: str
    company_name: str
    user_role: UserRole
    access: Access
    csrf_token: str


class DemoSwitch(ContractModel):
    company_id: str = Field(max_length=64)
    user_role: UserRole


class Feed(ContractModel):
    items: list[FeedItem]
    completeness: float = Field(ge=0, le=1)
    low_data: bool


class EventDetail(ContractModel):
    event: Event
    sources: list[SourceFragment]
    market: list[MarketSeries]
    bank: list[ThesisItem]


class Why(ContractModel):
    score: RelevanceScore
    chips: list[WhyChip]


class InsightStart(ContractModel):
    insight_id: int
    status: InsightStatus
    cached: bool


class Trace(ContractModel):
    insight_id: int
    steps: list[AgentStep]


# В запросах необязательные поля имеют значения по умолчанию: клиенту не нужно слать null.
class ChatRequest(ContractModel):
    chat_id: UUID | None = None
    event_id: int | None = None
    question: str = Field(min_length=1, max_length=500)


class NewsCheck(ContractModel):
    text: str | None = Field(default=None, max_length=20000)
    url: AnyHttpUrl | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "NewsCheck":
        if (self.text is None) == (self.url is None):
            raise ValueError("нужно передать ровно одно из полей: text или url")
        return self


class NewsCheckResult(ContractModel):
    event_id: int
    insight_id: int
    relevance: RelevanceScore


class Profile(ContractModel):
    profile: CompanyProfile
    suppliers: list[CounterpartyShare]
    customers: list[CounterpartyShare]


class ProfilePatch(ContractModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[A-Za-z0-9_]+)*$", max_length=100)
    value: JsonValue


class ProfileText(ContractModel):
    text: str = Field(min_length=1, max_length=2000)


class ProfileTextPreview(ContractModel):
    before: list[Exposure]
    after: list[Exposure]


class CompareSide(ContractModel):
    company_id: str
    feed: list[FeedItem]
    insight: InsightResult | None


class Compare(ContractModel):
    event_id: int
    a: CompareSide
    b: CompareSide


class FeedbackIn(ContractModel):
    event_id: int
    insight_id: int | None = None
    verdict: FeedbackVerdict
    reason: FeedbackReason | None = None


class ReviewIn(ContractModel):
    insight_id: int
    action: ExpertAction
    patch: dict[str, JsonValue] | None = None
    comment: str | None = Field(default=None, max_length=1000)


class ReviewQueueItem(ContractModel):
    insight_id: int
    event_id: int
    company_id: str
    reason: str = Field(max_length=200)
    created_at: AwareDatetime


class ApproachMetrics(ContractModel):
    name: str = Field(max_length=120)
    metrics: dict[str, float]


class Quality(ContractModel):
    approaches: list[ApproachMetrics]
    current: dict[str, float]


class TaskUsage(ContractModel):
    task: Slug
    model: str = Field(max_length=60)
    calls: int = Field(ge=0)
    cost_rub: float = Field(ge=0)
    fallbacks: int = Field(ge=0)
    errors: int = Field(ge=0)
    p50_ms: int = Field(ge=0)


class LlmUsage(ContractModel):
    day: dt.date
    budget_rub: float = Field(ge=0)
    spent_rub: float = Field(ge=0)
    ladder_step: Annotated[int, Field(ge=0, le=4)]
    by_task: list[TaskUsage]


class ToolCallLog(ContractModel):
    id: int
    user_id: int
    tool: Slug
    args: dict[str, JsonValue]
    result_chars: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    ok: bool
    created_at: AwareDatetime


class SourceHealth(ContractModel):
    id: Slug
    name: str = Field(max_length=120)
    kind: SourceKind
    enabled: bool
    last_ok_at: AwareDatetime | None
    last_error: str | None = Field(max_length=300)
    items_24h: int = Field(ge=0)


class ThesisVerify(ContractModel):
    verified: bool
