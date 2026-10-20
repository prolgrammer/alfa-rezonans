import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.types import Slug


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketPoint(_Model):
    date: dt.date
    value: float


class MarketSeries(_Model):
    series: Slug
    name: str = Field(max_length=120)
    unit: str = Field(max_length=20)
    source: str = Field(max_length=60)
    points: list[MarketPoint]
    value: float | None
    change_day: float | None
    change_week: float | None
    change_month: float | None
