import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts.enums import (
    ChatAuthor,
    Confidence,
    EvidenceType,
    FactorDirection,
    ForecastMatch,
    Horizon,
    ImpactDirection,
    InsightMode,
    Magnitude,
    Origin,
)
from app.contracts.market import MarketSeries
from app.contracts.types import EvidenceId, Slug


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewsRef(_Model):
    kind: Literal["news"] = "news"
    event_id: int
    raw_item_id: int
    url: str
    source_name: str = Field(max_length=120)
    published_at: dt.datetime


class BankRef(_Model):
    kind: Literal["bank"] = "bank"
    doc_id: int
    page: int = Field(ge=1)
    thesis_id: int | None
    published_on: dt.date


class MarketRef(_Model):
    kind: Literal["market"] = "market"
    series: Slug
    date: dt.date


class FeatureRef(_Model):
    kind: Literal["feature"] = "feature"
    feature_key: str
    source: str


Ref = Annotated[NewsRef | BankRef | MarketRef | FeatureRef, Field(discriminator="kind")]


class Evidence(_Model):
    id: EvidenceId
    type: EvidenceType
    text: str = Field(max_length=1500)
    ref: Ref

    @model_validator(mode="after")
    def _ref_matches_type(self) -> "Evidence":
        if self.ref.kind != self.type.value:
            raise ValueError(f"ссылка вида {self.ref.kind} не подходит доказательству типа {self.type.value}")
        return self


class Statement(_Model):
    id: str = Field(pattern=r"^s\d+$")
    text: str = Field(max_length=400)
    origin: Origin
    # пустой список схема допускает: отсутствие опоры ловит проверка V2, а не валидация формы
    evidence: list[EvidenceId] = Field(max_length=6)
    confidence: Confidence


class Link(Statement):
    feature_keys: list[str] = Field(max_length=5)


class Impact(_Model):
    direction: ImpactDirection
    strength: Magnitude
    horizon: Horizon


class Scenario(_Model):
    condition: str = Field(max_length=300)
    outcome: str = Field(max_length=300)
    evidence: list[EvidenceId]


class BankView(_Model):
    statements: list[Statement] = Field(max_length=4)
    as_of: dt.date | None
    match: ForecastMatch
    gap: str | None = Field(max_length=300)


class Offer(_Model):
    product_id: Slug
    text: str = Field(max_length=300)


class Insight(_Model):
    headline: str = Field(max_length=110)
    impact: Impact
    what_happened: list[Statement] = Field(max_length=6)
    assumptions: list[Statement] = Field(max_length=6)
    why_it_matters: list[Link] = Field(max_length=6)
    risks: list[Statement] = Field(max_length=6)
    opportunities: list[Statement] = Field(max_length=6)
    watch: list[Statement] = Field(max_length=4)
    scenarios: list[Scenario] = Field(max_length=2)
    actions: list[Statement] = Field(max_length=5)
    questions: list[Annotated[str, Field(max_length=200)]] = Field(max_length=5)
    bank_view: BankView
    bank_offer: Offer | None
    confidence: Confidence
    limitations: list[Annotated[str, Field(max_length=300)]] = Field(max_length=5)

    def statements(self) -> list[Statement]:
        return [
            *self.what_happened,
            *self.assumptions,
            *self.why_it_matters,
            *self.risks,
            *self.opportunities,
            *self.watch,
            *self.actions,
            *self.bank_view.statements,
        ]


class InsightChecks(_Model):
    statements_total: int = Field(ge=0)
    passed_first_try: int = Field(ge=0)
    removed: int = Field(ge=0)
    revisions: int = Field(ge=0, le=2)


class InsightResult(_Model):
    insight: Insight
    evidence: list[Evidence]
    mode: InsightMode
    checks: InsightChecks

    @model_validator(mode="after")
    def _evidence_exists(self) -> "InsightResult":
        known = {e.id for e in self.evidence}
        if len(known) != len(self.evidence):
            raise ValueError("идентификаторы доказательств повторяются")
        used = {ref for s in self.insight.statements() for ref in s.evidence}
        used |= {ref for sc in self.insight.scenarios for ref in sc.evidence}
        missing = sorted(used - known)
        if missing:
            raise ValueError(f"в доказательствах нет {', '.join(missing)}")
        return self


class Chain(_Model):
    """Цепочка «фактор события × чувствительность компании» (D2)."""

    factor: Slug
    event_direction: FactorDirection
    company_sign: Literal[-1, 0, 1]
    expected_effect: ImpactDirection
    channel: str = Field(max_length=200)
    strength: int = Field(ge=1, le=3)
    because: list[str]


class ChatAnswer(_Model):
    text: str = Field(max_length=2000)
    evidence: list[Evidence]
    series: MarketSeries | None
    suggestions: list[Annotated[str, Field(max_length=120)]] = Field(max_length=5)


class ChatTurn(_Model):
    author: ChatAuthor
    text: str = Field(max_length=2000)
