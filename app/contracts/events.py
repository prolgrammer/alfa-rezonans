import datetime as dt
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.enums import (
    CardStatus,
    Certainty,
    EventType,
    FactorDirection,
    Horizon,
    Magnitude,
    SectorRole,
)
from app.contracts.types import Slug

Cites = Annotated[list[Annotated[int, Field(ge=1, le=5)]], Field(min_length=1, max_length=5)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CardFact(_Model):
    text: str = Field(max_length=400)
    cites: Cites
    quote: str = Field(max_length=500)


class CardClaim(_Model):
    text: str = Field(max_length=400)
    who: str = Field(max_length=120)
    cites: Cites
    quote: str = Field(max_length=500)


class CardDate(_Model):
    what: str = Field(max_length=120)
    date: dt.date


class CardNumber(_Model):
    what: str = Field(max_length=120)
    value: float
    unit: str = Field(max_length=20)
    quote: str = Field(max_length=500)


class CardCause(_Model):
    text: str = Field(max_length=400)
    cites: Cites


class CardFactor(_Model):
    id: Slug
    direction: FactorDirection
    magnitude: Magnitude
    quote: str = Field(max_length=500)


class CardSector(_Model):
    id: Slug
    role: SectorRole


class EventCard(_Model):
    summary: str = Field(max_length=600)
    facts: list[CardFact] = Field(max_length=10)
    claims: list[CardClaim] = Field(max_length=10)
    actors: list[Annotated[str, Field(max_length=120)]] = Field(max_length=15)
    dates: list[CardDate] = Field(max_length=10)
    numbers: list[CardNumber] = Field(max_length=15)
    causes: list[CardCause] = Field(max_length=5)
    event_type: EventType
    factors: list[CardFactor] = Field(max_length=8)
    sectors: list[CardSector] = Field(max_length=10)
    regions: list[Annotated[str, Field(max_length=60)]] = Field(max_length=10)
    horizon: Horizon
    certainty: Certainty


class Event(_Model):
    id: int
    title: str = Field(max_length=300)
    first_seen_at: dt.datetime
    last_item_at: dt.datetime
    source_count: int = Field(ge=1)
    has_official: bool
    factors: list[Slug]
    sectors: list[Slug]
    regions: list[str]
    card: EventCard | None
    card_status: CardStatus


class NewsFilterResult(_Model):
    raw_item_id: int
    business: bool
    reason: str = Field(max_length=200)
