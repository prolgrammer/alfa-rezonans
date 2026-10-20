import datetime as dt
from typing import Literal

from pydantic import AwareDatetime, Field

from app.contracts._base import ContractModel
from app.contracts.enums import FeedFilter, RelevanceStage
from app.contracts.insight import Impact
from app.contracts.types import Slug


class RelevanceTerms(ContractModel):
    exposure: float = Field(ge=0, le=1)
    sector: float = Field(ge=0, le=1)
    counterparties: float = Field(ge=0, le=1)
    region: float = Field(ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    bank: float = Field(ge=0, le=1)
    freshness: float = Field(ge=0, le=1)
    user: float = Field(ge=0, le=1)
    penalty: float = Field(ge=0, le=1)


class RelevanceScore(ContractModel):
    event_id: int
    score: float
    terms: RelevanceTerms
    stage: RelevanceStage
    reason: str | None = Field(max_length=200)
    judge: float | None = Field(ge=0, le=3)


class WhyChip(ContractModel):
    term: Literal[
        "exposure", "sector", "counterparties", "region", "importance", "bank", "freshness", "user", "penalty"
    ]
    text: str = Field(max_length=80)
    feature_keys: list[str]


class FeedItem(ContractModel):
    event_id: int
    title: str = Field(max_length=300)
    headline: str | None = Field(max_length=110)
    impact: Impact | None
    why: list[WhyChip] = Field(max_length=3)
    bank_line: str | None = Field(max_length=200)
    bank_line_date: dt.date | None
    main_factor: Slug | None
    factors: list[Slug]
    source_count: int = Field(ge=1)
    has_official: bool
    last_item_at: AwareDatetime
    important_for_all: bool
    insight_id: int | None
    score: float


class JudgeScore(ContractModel):
    event_id: int
    relevance: int = Field(ge=0, le=3)
    why: str = Field(max_length=200)


class FeedFilters(ContractModel):
    impact: FeedFilter = FeedFilter.all
    factor: Slug | None = None
