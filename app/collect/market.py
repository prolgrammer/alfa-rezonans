import datetime as dt

from pydantic import TypeAdapter

from app.contracts import MarketSeries
from app.errors import NotFound
from app.fixtures import load_fixture

_SERIES = TypeAdapter(list[MarketSeries])


# заглушка до BE-5
async def get_market_series(series: str, days: int) -> MarketSeries:
    for item in load_fixture("market.json", _SERIES):
        if item.series == series:
            # окно считаем от последней точки ряда: фикстура зафиксирована на дату сюжета
            cutoff = item.points[-1].date - dt.timedelta(days=days)
            return item.model_copy(update={"points": [p for p in item.points if p.date >= cutoff]})
    raise NotFound(f"ряд {series} не найден")
