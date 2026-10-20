from pydantic import TypeAdapter

from app.contracts import Event, EventCard, NewsFilterResult, RawItemIn
from app.fixtures import load_fixture

_EVENTS = TypeAdapter(list[Event])


# заглушка до ML-4
async def news_filter(items: list[RawItemIn]) -> list[NewsFilterResult]:
    return [
        NewsFilterResult(raw_item_id=item.id, business=True, reason="заглушка: фильтр по правилам")
        for item in items
    ]


# заглушка до ML-5
async def extract_event_card(event_id: int) -> EventCard | None:
    for event in load_fixture("events.json", _EVENTS):
        if event.id == event_id:
            return event.card
    return None
