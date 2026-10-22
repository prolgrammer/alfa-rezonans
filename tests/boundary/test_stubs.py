import datetime as dt

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from app.agent import run, tools
from app.agent.run import run_chat, run_insight
from app.agent.tools import build_tools
from app.bank.theses import fixture_bank_view
from app.clients.counterparties import get_counterparties, get_sector_exposure
from app.clients.exposures import exposures
from app.clients.profile import feature_phrase, get_profile, profile_compact
from app.collect.market import get_market_series
from app.contracts import (
    Access,
    AgentStep,
    ChatAnswer,
    ChatTurn,
    CounterpartyShare,
    Event,
    Exposure,
    FeedFilters,
    FeedItem,
    InsightResult,
    JudgeScore,
    NewsFilterResult,
    NewsHit,
    Period,
    RawItemIn,
    RelevanceScore,
    SessionCtx,
    ThesisItem,
    UserRole,
)
from app.fixtures import load_fixture
from app.llm.gateway import LLMUnavailable, ToolSpec, gw
from app.news.extract import CardHints, build_event_card, news_filter
from app.news.search import get_event_sources, search_bank_chunks, search_news
from app.rank.feed import explain, highlights, rank_feed, warm_list
from app.rank.judge import rank_judge
from app.settings import get_settings

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
    found = get_sector_exposure("metallurgy", "export_duties")
    assert isinstance(found, Exposure)
    assert (found.factor, found.sign, found.strength) == ("export_duties", -1, 3)
    assert get_sector_exposure("metallurgy", "no_such_factor") is None
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


@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(run, "STEP_DELAY_S", (0, 0))


async def test_gateway_without_key_is_unavailable():
    # ключ обнулён в tests/conftest.py, поэтому шлюз отказывает до обращения к сети
    with pytest.raises(LLMUnavailable):
        await gw.tools_step(
            "chat", [{"role": "user", "content": "x"}], [ToolSpec(name="t", description="d", parameters={})]
        )


async def test_news_filter_passes_everything():
    item = RawItemIn(
        id=7,
        source_id="cbr",
        title="Новость",
        summary=None,
        url="https://example.test/1",
        published_at="2026-10-20T10:00:00+03:00",
    )
    (result,) = await news_filter([item])
    assert isinstance(result, NewsFilterResult)
    assert (result.raw_item_id, result.business) == (7, True)


async def test_event_card_without_llm_raises():
    # без ключа модель недоступна: extract_event_card превращает это в card_status=failed
    sources = await get_event_sources(1)
    with pytest.raises(LLMUnavailable):
        await build_event_card("Банк России сохранил ключевую ставку", CardHints(), sources)
    assert await build_event_card("Без публикаций", CardHints(), []) is None


@pytest.fixture(autouse=True)
def bank_view_from_fixture(monkeypatch):
    # инструменты агента здесь проверяются без БД: позиция банка — из фикстуры, как в MOCK
    monkeypatch.setattr(tools, "get_bank_view", fixture_bank_view)


async def test_fixture_bank_view_is_verified_and_fresh():
    items = await fixture_bank_view("key_rate")
    assert items and len(items) <= 3
    assert all(isinstance(i, ThesisItem) and i.verified is True for i in items)
    dates = [i.published_on for i in items]
    assert dates == sorted(dates, reverse=True)
    assert len(await fixture_bank_view("key_rate", limit=1)) == 1


async def test_rank_judge_stub_is_empty():
    compact = profile_compact(await get_profile("C-DEV-01"), UserRole.cfo)
    card = next(e.card for e in load_fixture("events.json", TypeAdapter(list[Event])) if e.id == 1)
    result: list[JudgeScore] = await rank_judge(compact, [(1, card)])
    assert result == []


async def test_run_insight_sequence(no_delay, monkeypatch):
    # без ключа разбор краткий; позиция банка — из фикстуры, чтобы тест не ходил в БД
    monkeypatch.setattr(run, "_bank_view", fixture_bank_view)
    items = [x async for x in run_insight(DEV, 1)]
    *steps, last = items
    assert steps and all(isinstance(s, AgentStep) for s in steps)
    assert [s.seq for s in steps] == list(range(1, len(steps) + 1))
    assert isinstance(last, InsightResult)


async def test_run_insight_unknown_event_fails_before_first_step(no_delay):
    with pytest.raises(LookupError):
        await anext(run_insight(DEV, 999))


async def test_run_chat_sequence(no_delay):
    history = [ChatTurn(author="user", text="Привет")]
    items = [x async for x in run_chat(DEV, 1, history, "Что говорит банк?")]
    *steps, last = items
    assert [s.kind for s in steps] == ["plan", "tool", "final"]
    assert [s.seq for s in steps] == [1, 2, 3]
    assert isinstance(last, ChatAnswer)
    assert isinstance([x async for x in run_chat(DEV, None, [], "Вопрос")][-1], ChatAnswer)
    with pytest.raises(LookupError):
        await anext(run_chat(DEV, 999, [], "Вопрос"))


VALID_ARGS = {
    "get_company_profile": {"section": "overview"},
    "get_counterparties": {"direction": "suppliers", "top": 3},
    "get_bank_view": {"factor": "key_rate"},
    "search_bank_research": {"query": "ставка", "factor": "key_rate"},
    "get_market_data": {"series": "cbr_key_rate", "days": 30},
    "search_news": {"query": "ставка ЦБ"},
    "get_event_sources": {"hint": "решение"},
    "get_sector_exposure": {"sector": "metallurgy", "factor": "steel"},
}


async def test_build_tools_has_exactly_the_nine_tools():
    assert set(build_tools(DEV, 1)) == {*VALID_ARGS, "submit_insight"}


@pytest.mark.parametrize("name", sorted(VALID_ARGS))
async def test_tool_run_returns_contract_models(name):
    tool = build_tools(DEV, 1)[name]
    result = await tool.run(tool.args.model_validate(VALID_ARGS[name]))
    items = result if isinstance(result, list) else [result]
    assert items and all(isinstance(i, BaseModel) for i in items)


async def test_submit_insight_returns_draft():
    raw = (get_settings().fixtures_dir / "insight.json").read_bytes()
    draft = TypeAdapter(InsightResult).validate_json(raw).insight
    tool = build_tools(DEV, 1)["submit_insight"]
    assert await tool.run(tool.args(draft=draft)) == draft


async def test_company_profile_sections():
    tool = build_tools(DEV, 1)["get_company_profile"]

    async def keys(section: str) -> list[str]:
        return [f.key for f in await tool.run(tool.args(section=section))]

    overview = await keys("overview")
    assert "sector" in overview
    assert not [k for k in overview if k.startswith(("products.", "group.", "regions"))]
    assert await keys("products") == ["products.LOA", "products.GAR", "products.DEP", "products.LEA"]
    exposures_result = await tool.run(tool.args(section="exposures"))
    assert exposures_result == (await get_profile("C-DEV-01")).exposures


async def test_tools_read_company_from_session():
    dev = build_tools(DEV, 1)["get_company_profile"]
    it = build_tools(IT, 1)["get_company_profile"]
    args = dev.args(section="overview")
    assert await dev.run(args) != await it.run(args)


async def test_search_bank_research_without_factor_has_no_theses():
    tool = build_tools(DEV, 1)["search_bank_research"]
    result = await tool.run(tool.args(query="ставка"))
    assert result.theses == [] and result.chunks


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("get_counterparties", {"direction": "suppliers", "top": 11}),
        ("get_counterparties", {"direction": "suppliers", "top": 0}),
        ("get_market_data", {"series": "cbr_key_rate", "days": 366}),
        ("get_market_data", {"series": "cbr_key_rate", "days": 0}),
        ("search_news", {"query": "ставка", "days": 91}),
        ("search_news", {"query": "а" * 201}),
        ("search_bank_research", {"query": "а" * 201}),
        ("get_event_sources", {"hint": "а" * 201}),
        ("get_bank_view", {"factor": "Key Rate"}),
    ],
)
def test_tool_args_reject_out_of_range_values(name, args):
    with pytest.raises(ValidationError):
        build_tools(DEV, 1)[name].args.model_validate(args)


def test_tool_args_accept_boundary_values():
    tools = build_tools(DEV, 1)
    tools["get_counterparties"].args.model_validate({"direction": "suppliers", "top": 10})
    tools["get_market_data"].args.model_validate({"series": "cbr_key_rate", "days": 365})
    tools["search_news"].args.model_validate({"query": "а" * 200, "days": 90})


@pytest.mark.parametrize("name", sorted(VALID_ARGS))
def test_tool_args_reject_company_id(name):
    with pytest.raises(ValidationError):
        build_tools(DEV, 1)[name].args.model_validate({**VALID_ARGS[name], "company_id": "C-IT-01"})


def test_submit_insight_args_reject_company_id():
    tool = build_tools(DEV, 1)["submit_insight"]
    raw = (get_settings().fixtures_dir / "insight.json").read_bytes()
    draft = TypeAdapter(InsightResult).validate_json(raw).insight.model_dump(mode="json")
    tool.args.model_validate({"draft": draft})
    with pytest.raises(ValidationError):
        tool.args.model_validate({"draft": draft, "company_id": "C-IT-01"})
