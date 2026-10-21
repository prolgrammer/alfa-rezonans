import datetime as dt

from pydantic import TypeAdapter

from app.contracts import BankChunk, NewsHit, SourceFragment, ThesisItem
from app.errors import NotFound
from app.fixtures import load_fixture

_HITS = TypeAdapter(list[NewsHit])
_CHUNKS = TypeAdapter(list[BankChunk])
_SOURCES = TypeAdapter(list[SourceFragment])
_THESES = TypeAdapter(list[ThesisItem])


# заглушка до BE-7
async def search_news(query: str, days: int = 90, factor: str | None = None) -> list[NewsHit]:
    # Даты фикстур привязаны к сюжету, а не к часам: фильтр по days оставил бы пустой ответ.
    return sorted(load_fixture("news_hits.json", _HITS), key=lambda h: -h.score)


# заглушка до BE-7
async def search_bank_chunks(
    query: str, factor: str | None = None, since: dt.date | None = None
) -> list[BankChunk]:
    chunks = load_fixture("bank_chunks.json", _CHUNKS)
    if factor is not None:
        # у фрагмента нет факторов: берём их из тезисов того же документа
        docs = {t.doc_id for t in load_fixture("theses.json", _THESES) if factor in t.thesis.factors}
        chunks = [c for c in chunks if c.doc_id in docs]
    if since is not None:
        chunks = [c for c in chunks if c.published_on and c.published_on >= since]
    return chunks


# заглушка до BE-7
async def get_event_sources(event_id: int, hint: str | None = None) -> list[SourceFragment]:
    rows = [s for s in load_fixture("sources.json", _SOURCES) if s.event_id == event_id]
    if not rows:
        raise NotFound(f"публикации события {event_id} не найдены")
    return sorted(rows, key=lambda s: s.n)
