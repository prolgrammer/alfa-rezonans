import re

import pytest
from pydantic import TypeAdapter

from app.api.schemas import LlmUsage, Quality, ReviewQueueItem, SourceHealth, ToolCallLog
from app.catalogs import factor_ids, sector_ids
from app.contracts import (
    AgentStep,
    BankChunk,
    BankRef,
    ChatAnswer,
    CompanyProfile,
    CounterpartyDirection,
    CounterpartyShare,
    Event,
    Exposure,
    FeatureRef,
    FeedItem,
    InsightResult,
    MarketRef,
    MarketSeries,
    NewsHit,
    NewsRef,
    RelevanceScore,
    SourceFragment,
    StepKind,
    ThesisItem,
)
from app.fixtures import load_fixture
from app.settings import get_settings

REGISTRY: dict[str, TypeAdapter] = {
    "events.json": TypeAdapter(list[Event]),
    "sources.json": TypeAdapter(list[SourceFragment]),
    "profiles.json": TypeAdapter(list[CompanyProfile]),
    "counterparties.json": TypeAdapter(dict[str, dict[CounterpartyDirection, list[CounterpartyShare]]]),
    "sector_exposures.json": TypeAdapter(dict[str, list[Exposure]]),
    "theses.json": TypeAdapter(list[ThesisItem]),
    "bank_chunks.json": TypeAdapter(list[BankChunk]),
    "market.json": TypeAdapter(list[MarketSeries]),
    "news_hits.json": TypeAdapter(list[NewsHit]),
    "feed.json": TypeAdapter(dict[str, list[FeedItem]]),
    "relevance.json": TypeAdapter(dict[str, list[RelevanceScore]]),
    "insight.json": TypeAdapter(InsightResult),
    "insight_it.json": TypeAdapter(InsightResult),
    "trace.json": TypeAdapter(list[AgentStep]),
    "trace_it.json": TypeAdapter(list[AgentStep]),
    "chat_answer.json": TypeAdapter(ChatAnswer),
    "llm_usage.json": TypeAdapter(LlmUsage),
    "quality.json": TypeAdapter(Quality),
    "tool_calls.json": TypeAdapter(list[ToolCallLog]),
    "sources_health.json": TypeAdapter(list[SourceHealth]),
    "review_queue.json": TypeAdapter(list[ReviewQueueItem]),
}


def fx(name: str):
    return load_fixture(name, REGISTRY[name])


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("ё", "е")).strip().lower()


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_fixture_valid(name):
    assert fx(name)


def test_no_unregistered_fixtures():
    on_disk = {p.name for p in get_settings().fixtures_dir.glob("*.json")}
    assert on_disk == set(REGISTRY)


def test_bank_page_is_png():
    assert (get_settings().fixtures_dir / "bank_page.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_fixture_ids_in_catalogs():
    factors: set[str] = set()
    sectors: set[str] = set()

    for event in fx("events.json"):
        factors |= set(event.factors)
        sectors |= set(event.sectors)
        if event.card:
            factors |= {f.id for f in event.card.factors}
            sectors |= {s.id for s in event.card.sectors}
    for thesis in fx("theses.json"):
        factors |= set(thesis.thesis.factors)
    exposures = [e for p in fx("profiles.json") for e in p.exposures]
    exposures += [e for rows in fx("sector_exposures.json").values() for e in rows]
    factors |= {e.factor for e in exposures}
    for items in fx("feed.json").values():
        for item in items:
            factors |= set(item.factors)
            if item.main_factor:
                factors.add(item.main_factor)
    for profile in fx("profiles.json"):
        sectors |= {f.value for f in profile.features if f.key == "sector"}
    for by_direction in fx("counterparties.json").values():
        sectors |= {c.sector for rows in by_direction.values() for c in rows}
    sectors |= set(fx("sector_exposures.json"))

    assert factors <= factor_ids(), factors - factor_ids()
    assert sectors <= sector_ids(), sectors - sector_ids()


def test_cross_refs():
    event_ids = {e.id for e in fx("events.json")}
    company_ids = {p.company_id for p in fx("profiles.json")}

    assert {s.event_id for s in fx("sources.json")} <= event_ids
    for name in ("feed.json", "relevance.json", "counterparties.json"):
        assert set(fx(name)) <= company_ids, name
    for items in fx("feed.json").values():
        assert {i.event_id for i in items} <= event_ids
    for rows in fx("relevance.json").values():
        assert {r.event_id for r in rows} <= event_ids

    theses = {t.id: t for t in fx("theses.json")}
    sources = {(s.event_id, s.raw_item_id) for s in fx("sources.json")}
    series = {s.series: {p.date for p in s.points} for s in fx("market.json")}
    keys = {p.company_id: {f.key for f in p.features} for p in fx("profiles.json")}
    # разбор и чат принадлежат компании: ссылки на признаки сверяем с её профилем
    owned = [
        (fx("insight.json"), "C-DEV-01"),
        (fx("insight_it.json"), "C-IT-01"),
        (fx("chat_answer.json"), "C-DEV-01"),
    ]
    for result, company in owned:
        check_refs(result, keys[company], event_ids, sources, theses, series)
    for result, company in owned[:2]:
        for link in result.insight.why_it_matters:
            assert set(link.feature_keys) <= keys[company]


def check_refs(result, company_keys, event_ids, sources, theses, series):
    for ev in result.evidence:
        ref = ev.ref
        if isinstance(ref, NewsRef):
            assert ref.event_id in event_ids
            assert (ref.event_id, ref.raw_item_id) in sources
        elif isinstance(ref, BankRef):
            thesis = theses[ref.thesis_id]
            assert (thesis.doc_id, thesis.thesis.page) == (ref.doc_id, ref.page)
            assert thesis.published_on == ref.published_on
        elif isinstance(ref, MarketRef):
            assert ref.date in series[ref.series]
        elif isinstance(ref, FeatureRef):
            assert ref.feature_key in company_keys


def test_card_quotes_in_sources():
    texts: dict[int, dict[int, str]] = {}
    for s in fx("sources.json"):
        texts.setdefault(s.event_id, {})[s.n] = norm(s.text)
    for event in fx("events.json"):
        card, by_n = event.card, texts[event.id]
        assert card
        everything = " ".join(by_n.values())
        for item in [*card.facts, *card.claims, *card.causes]:
            assert max(item.cites) <= len(by_n)
            quote = getattr(item, "quote", None)
            if quote:
                assert norm(quote) in " ".join(by_n[n] for n in item.cites), quote
        for item in [*card.numbers, *card.factors]:
            assert norm(item.quote) in everything, item.quote


def test_thesis_quotes_in_chunks():
    chunks = {(c.doc_id, c.page): norm(c.text) for c in fx("bank_chunks.json")}
    for item in fx("theses.json"):
        key = (item.doc_id, item.thesis.page)
        if key in chunks:
            assert norm(item.thesis.quote) in chunks[key], item.id


def test_event_aggregates_match_sources():
    by_event: dict[int, list[SourceFragment]] = {}
    for s in fx("sources.json"):
        by_event.setdefault(s.event_id, []).append(s)
    for event in fx("events.json"):
        rows = by_event[event.id]
        assert event.source_count == len(rows)
        assert event.has_official == any(r.is_official for r in rows)
        assert event.last_item_at == max(r.published_at for r in rows)


def test_profiles_are_rich_and_opposite_on_key_rate():
    profiles = {p.company_id: p for p in fx("profiles.json")}
    for profile in profiles.values():
        assert len(profile.features) >= 8
        assert len(profile.exposures) >= 5
    sign = {cid: next(e.sign for e in p.exposures if e.factor == "key_rate") for cid, p in profiles.items()}
    assert sign["C-DEV-01"] == -sign["C-IT-01"] != 0


def test_it_trace_is_well_formed():
    steps = fx("trace_it.json")
    assert [s.seq for s in steps] == list(range(1, len(steps) + 1))
    assert all(s.tool_call for s in steps if s.kind == StepKind.tool)
    assert steps[-1].kind == StepKind.final


def test_it_insight_differs_from_dev_insight():
    dev, it = fx("insight.json").insight, fx("insight_it.json").insight
    assert dev.impact.direction == "risk"
    assert it.impact.direction == "opportunity"
    assert it.headline == "Доходность размещения свободных денег сохранится дольше, чем ожидалось"
    checks = fx("insight_it.json").checks
    assert checks.statements_total - checks.removed == len(it.statements())


def test_trace_covers_every_step_kind():
    steps = fx("trace.json")
    assert {s.kind for s in steps} == set(StepKind)
    assert [s.seq for s in steps] == list(range(1, len(steps) + 1))
    assert all(s.tool_call for s in steps if s.kind == StepKind.tool)


def test_thesis_count_and_case_quote():
    theses = fx("theses.json")
    assert len(theses) == 10
    assert any(
        t.thesis.quote == "Ключевая ставка ЦБ РФ: ставка без изменения на 14,0%, риторика стала жестче"
        for t in theses
    )
