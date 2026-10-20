import datetime as dt

import pytest

from app.clients.counterparties import get_counterparties, get_sector_exposure
from app.clients.exposures import exposures
from app.clients.profile import feature_phrase, get_profile, profile_compact
from app.collect.market import get_market_series
from app.contracts import (
    Access,
    CounterpartyShare,
    Exposure,
    FeedFilters,
    FeedItem,
    NewsHit,
    Period,
    RelevanceScore,
    SessionCtx,
    UserRole,
)
from app.news.search import get_event_sources, search_bank_chunks, search_news
from app.rank.feed import explain, highlights, rank_feed, warm_list

DEV = SessionCtx(user_id=1, company_id="C-DEV-01", user_role=UserRole.cfo, access=Access.client)
IT = SessionCtx(user_id=2, company_id="C-IT-01", user_role=UserRole.cfo, access=Access.client)


async def test_get_profile():
    assert (await get_profile("C-DEV-01")).company_id == "C-DEV-01"
    with pytest.raises(LookupError):
        await get_profile("X")


async def test_profile_compact_top_by_strength():
    profile = await get_profile("C-DEV-01")
    compact = profile_compact(profile, UserRole.cfo)
    assert compact.user_role == UserRole.cfo
    assert compact.sector == "development"
    assert compact.scale == "15–50 млрд"
    assert len(compact.top_exposures) <= 8
    strengths = [e.strength for e in compact.top_exposures]
    assert strengths == sorted(strengths, reverse=True)


async def test_feature_phrase_and_exposures():
    assert feature_phrase("products.LOA", True) == "products.LOA: True"
    profile = await get_profile("C-IT-01")
    assert exposures(profile, {}) == profile.exposures


async def test_counterparties():
    rows = await get_counterparties(DEV, "suppliers", top=1)
    assert len(rows) == 1
    assert isinstance(rows[0], CounterpartyShare)
    sector = rows[0].sector
    assert {r.sector for r in await get_counterparties(DEV, "suppliers", sector=sector)} == {sector}
    with pytest.raises(LookupError):
        await get_counterparties(DEV.model_copy(update={"company_id": "X"}), "suppliers")


def test_sector_exposure():
    found = get_sector_exposure("metallurgy", "steel")
    assert found is None or isinstance(found, Exposure)
    assert get_sector_exposure("no_such_sector", "steel") is None


async def test_feed_filters():
    everything = await rank_feed(DEV, Period.week, FeedFilters())
    assert [i.event_id for i in everything] == [1, 3, 2]
    assert all(isinstance(i, FeedItem) for i in everything)
    risks = await rank_feed(DEV, Period.week, FeedFilters(impact="risk"))
    assert risks and all(i.impact and i.impact.direction == "risk" for i in risks)
    by_factor = await rank_feed(DEV, Period.week, FeedFilters(factor="steel"))
    assert by_factor and all("steel" in i.factors for i in by_factor)
    assert [i.event_id for i in await rank_feed(IT, Period.week, FeedFilters())] == [1, 2]


async def test_explain():
    score = await explain(DEV, 1)
    assert isinstance(score, RelevanceScore)
    assert score.event_id == 1
    with pytest.raises(LookupError):
        await explain(DEV, 999)


async def test_highlights_have_distinct_main_factors():
    for ctx in (DEV, IT):
        items = await highlights(ctx)
        factors = [i.main_factor for i in items]
        assert items and len(factors) == len(set(factors)) <= 3


async def test_warm_list():
    pairs = await warm_list()
    assert pairs
    assert {ctx.company_id for ctx, _ in pairs} == {"C-DEV-01", "C-IT-01"}
    assert all(ctx.user_id == 0 and ctx.user_role == UserRole.cfo and isinstance(e, int) for ctx, e in pairs)


async def test_search_news_hits():
    hits = await search_news("ставка")
    assert hits and all(isinstance(h, NewsHit) for h in hits)


async def test_search_bank_chunks_filters():
    everything = await search_bank_chunks("ставка")
    assert everything
    latest = max(c.published_on for c in everything if c.published_on)
    assert {c.published_on for c in await search_bank_chunks("ставка", since=latest)} == {latest}
    by_factor = await search_bank_chunks("ставка", factor="steel")
    assert len(by_factor) < len(everything)
    assert await search_bank_chunks("ставка", factor="no_such_factor") == []


async def test_get_event_sources():
    sources = await get_event_sources(1)
    assert [s.n for s in sources] == list(range(1, len(sources) + 1))
    with pytest.raises(LookupError):
        await get_event_sources(999)


async def test_market_series_cut_by_days():
    full = await get_market_series("cbr_key_rate", 365)
    cut = await get_market_series("cbr_key_rate", 30)
    assert 0 < len(cut.points) < len(full.points)
    assert min(p.date for p in cut.points) >= full.points[-1].date - dt.timedelta(days=30)
    assert cut.value == full.value
    with pytest.raises(LookupError):
        await get_market_series("unknown", 30)
