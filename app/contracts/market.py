import datetime as dt

from pydantic import Field

from app.contracts._base import ContractModel
from app.contracts.types import Slug


class MarketPoint(ContractModel):
    date: dt.date
    value: float


class MarketSeries(ContractModel):
    series: Slug
    name: str = Field(max_length=120)
    unit: str = Field(max_length=20)
    source: str = Field(max_length=60)
    points: list[MarketPoint]
    value: float | None
    # изменения — разность в единицах ряда (п. п. для ставки, ₽ для курса), не проценты
    change_day: float | None
    change_week: float | None
    change_month: float | None
