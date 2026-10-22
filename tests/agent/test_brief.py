import asyncio
import time

import pytest
from agent_helpers import EVENTS, PROFILES, event, session, sources

from app.agent import brief
from app.agent.brief import (
    BANK_PER_FACTOR,
    brief_result,
    build_brief,
    event_factor_order,
    reason_text,
    run_brief,
)
from app.agent.context import new_context
from app.agent.links import build_chains, event_moves
from app.agent.trace import TraceSink
from app.bank.theses import fixture_bank_view
from app.contracts import AgentStep, InsightResult, TraceRecord
from app.errors import NotFound
from app.llm import AllModelsFailed, BudgetExceeded, LLMNotConfigured, LLMUnavailable
from app.text.verify import mark_in, number_marks


async def run(company: str, ev, *, error=None, bank_view=fixture_bank_view, sink=None):
    items = [
        x
        async for x in run_brief(
            session(company),
            ev,
            sink=sink or TraceSink(),
            error=error or LLMNotConfigured("нет ключа"),
            bank_view=bank_view,
        )
    ]
    *steps, result = items
    assert all(isinstance(s, AgentStep) for s in steps) and isinstance(result, InsightResult)
    return steps, result


def assert_grounded(result: InsightResult) -> None:
    by_id = {e.id: e for e in result.evidence}
    for s in result.insight.statements():
        assert s.evidence
        types = {by_id[e].type for e in s.evidence}
        if s.origin == "fact":
            assert types <= {"news", "market"}
        if s.origin == "bank":
            assert types == {"bank"}
        marks = set().union(*(number_marks(by_id[e].text) for e in s.evidence))
        assert all(mark_in(m, marks) for m in number_marks(s.text)), s.text


def assert_contiguous(result: InsightResult) -> None:
    statements = result.insight.statements()
    assert [s.id for s in statements] == [f"s{i}" for i in range(1, len(statements) + 1)]
    assert [e.id for e in result.evidence] == [f"E{i}" for i in range(1, len(result.evidence) + 1)]
    used = {e for s in statements for e in s.evidence}
    assert used == {e.id for e in result.evidence}


@pytest.mark.parametrize("event_id", [1, 2, 3])
@pytest.mark.parametrize("company", ["C-DEV-01", "C-IT-01"])
async def test_brief_on_fixtures_is_valid(event_id, company):
    steps, result = await run(company, event(event_id))
    assert result.mode == "brief"
    assert [s.seq for s in steps] == list(range(1, len(steps) + 1))
    assert steps[0].kind == "fallback" and steps[-1].kind == "final"
    assert_grounded(result)
    assert_contiguous(result)
    assert result.insight.limitations[0].startswith("Краткий разбор: модель не подключена")
    assert result.checks.statements_total == len(result.insight.statements())


async def test_brief_case_developer():
    _, result = await run("C-DEV-01", event(1))
    insight = result.insight
    assert (insight.impact.direction, insight.impact.strength) == ("risk", "high")
    assert insight.headline.startswith("Процентная нагрузка не снизится")
    risks = [s.text for s in insight.risks]
    assert "Процентная нагрузка не снизится." in risks
    assert "Спрос на ипотеку не восстанавливается." in risks
    theses = await fixture_bank_view("key_rate")
    assert [s.text for s in insight.bank_view.statements] == [t.thesis.text for t in theses]
    assert len(theses) == BANK_PER_FACTOR
    # прогноз от 14.08 «снижение» против «ставка сохранена»; прогноз 12.09 вышел уже после события
    assert insight.bank_view.match == "partly"
    assert insight.bank_view.as_of == max(t.published_on for t in theses)
    true_keys = {f.key for f in PROFILES["C-DEV-01"].features if f.value}
    assert insight.why_it_matters
    for link in insight.why_it_matters:
        assert set(link.feature_keys) <= true_keys and len(link.feature_keys) <= 5
    assert insight.opportunities == [] or all(s.origin == "model" for s in insight.opportunities)
    assert insight.confidence == "medium"
    assert insight.scenarios == insight.actions == insight.questions == []
    assert insight.bank_offer is None
    assert insight.what_happened and insight.assumptions and insight.watch


async def test_brief_case_it():
    _, result = await run("C-IT-01", event(1))
    insight = result.insight
    assert insight.impact.direction == "opportunity"
    assert "Доходность размещения сохраняется." in [s.text for s in insight.opportunities]
    assert insight.risks == []
    assert insight.headline.startswith("Доходность размещения сохраняется")


async def test_brief_no_card():
    ev = event(1).model_copy(update={"card": None})
    steps, result = await run("C-DEV-01", ev)
    insight = result.insight
    assert insight.headline.startswith("Карточка события ещё не готова")
    assert insight.why_it_matters
    assert insight.risks == [] and insight.opportunities == []
    assert insight.what_happened == [] and insight.watch == []
    assert insight.confidence == "low"
    assert insight.impact.horizon == "months"
    assert any("Карточка события" in x for x in insight.limitations)
    assert_grounded(result)
    assert_contiguous(result)
    assert steps[1].kind == "links"


async def test_brief_no_profile(monkeypatch):
    async def missing(company_id):
        raise NotFound("нет профиля")

    monkeypatch.setattr(brief, "get_profile", missing)
    _, result = await run("C-DEV-01", event(1))
    insight = result.insight
    assert insight.headline == "Прямой связи события с профилем компании не найдено"
    assert insight.what_happened and insight.assumptions and insight.watch
    assert insight.why_it_matters == [] and insight.risks == []
    assert any("Профиль компании недоступен" in x for x in insight.limitations)
    assert insight.confidence == "low"
    assert insight.impact.direction == "neutral" and insight.impact.strength == "low"
    assert insight.bank_view.statements
    assert_grounded(result)


async def test_brief_no_link_with_profile():
    ev = event(2)
    _, result = await run("C-IT-01", ev)
    # у IT есть cny_rub (знак не определён), но не china_trade
    assert result.insight.impact.direction == "neutral"
    assert result.insight.headline == "Курс юаня к рублю: знак влияния не определён"

    other = event(1).model_copy(update={"card": None, "factors": ["fuel"]})
    _, result = await run("C-IT-01", other)
    assert any("нет факторов этого события" in x for x in result.insight.limitations)


async def test_brief_no_theses():
    async def empty(factor, limit):
        return []

    steps, result = await run("C-DEV-01", event(1), bank_view=empty)
    view = result.insight.bank_view
    assert view.statements == [] and view.as_of is None and view.match == "no_forecast"
    assert "Ключевая ставка ЦБ" in view.gap
    # опрошены все факторы события по порядку
    tool_steps = [s for s in steps if s.tool_call and s.tool_call.tool == "get_bank_view"]
    assert [s.tool_call.args["factor"] for s in tool_steps] == event_factor_order(event(1))
    assert any("нет вычитанной позиции по главному фактору" in x for x in result.insight.limitations)


async def test_brief_theses_from_secondary_factor():
    steps, result = await run("C-DEV-01", event(3))
    view = result.insight.bank_view
    steel = await fixture_bank_view("steel")
    assert [s.text for s in view.statements] == [t.thesis.text for t in steel]
    assert any(
        "По главному фактору «Экспортные пошлины» позиции банка нет" in x for x in result.insight.limitations
    )
    tool_steps = [s for s in steps if s.tool_call and s.tool_call.tool == "get_bank_view"]
    assert [s.tool_call.args["factor"] for s in tool_steps] == ["export_duties", "steel"]


async def test_brief_nothing_at_all():
    ev = event(1).model_copy(update={"card": None, "factors": []})
    ctx = new_context(ev, None, session("C-DEV-01").user_role, [])
    insight = build_brief(
        ctx,
        profile=None,
        sources=None,
        theses=[],
        theses_factor=None,
        bank_failed=False,
        reason="модель недоступна",
    )
    result = brief_result(ctx, insight)
    assert insight.headline.startswith("Карточка события ещё не готова")
    assert insight.bank_view.gap and insight.bank_view.statements == []
    assert len(insight.limitations) >= 3
    assert result.evidence == [] and result.checks.statements_total == 0


async def test_brief_skipped_items_leave_no_gaps():
    ev = event(1)
    profile = PROFILES["C-DEV-01"]
    chains = build_chains(event_moves(ev), profile.exposures)
    theses = await fixture_bank_view("key_rate")
    for available in (None, [], sources(1)[1:2]):
        ctx = new_context(ev, None, session("C-DEV-01").user_role, chains)
        insight = build_brief(
            ctx,
            profile=profile,
            sources=available,
            theses=theses,
            theses_factor="key_rate",
            bank_failed=False,
            reason="x",
        )
        result = brief_result(ctx, insight)
        assert_contiguous(result)
        assert_grounded(result)
        assert result.evidence
        if not available:
            assert insight.what_happened == [] and insight.watch == []
            assert any("Публикации события недоступны" in x for x in insight.limitations)
        else:
            # факт со ссылкой только на публикацию 1 пропущен, а нумерация осталась плотной
            assert len(insight.what_happened) < len(ev.card.facts) + len(ev.card.causes)


async def test_brief_respects_block_limits():
    ev = event(1)
    card = ev.card.model_copy(update={"facts": ev.card.facts * 5})
    ctx = new_context(ev.model_copy(update={"card": card}), None, session("C-DEV-01").user_role, [])
    insight = build_brief(
        ctx,
        profile=None,
        sources=sources(1),
        theses=[],
        theses_factor=None,
        bank_failed=False,
        reason="x",
    )
    assert len(insight.what_happened) == 6
    # доказательств ровно столько, сколько нужно шести утверждениям, а не десяти
    assert_contiguous(brief_result(ctx, insight))


def test_reason_text_by_error_class():
    assert reason_text(LLMNotConfigured("x")) == "модель не подключена"
    assert reason_text(BudgetExceeded("x")) == "исчерпан дневной бюджет на модели"
    assert reason_text(AllModelsFailed("x")) == "модели не ответили"
    assert reason_text(LLMUnavailable("x")) == "модель недоступна"
    assert reason_text(RuntimeError("x")) == "модель недоступна"
    assert reason_text(None) == "модель недоступна"


def test_event_factor_order_card_first_without_repeats():
    ev = event(1).model_copy(update={"factors": ["fuel", "key_rate"]})
    assert event_factor_order(ev) == ["key_rate", "inflation", "mortgage_housing", "fuel"]
    assert event_factor_order(ev.model_copy(update={"card": None})) == ["fuel", "key_rate"]


async def test_run_brief_survives_failing_reads(monkeypatch):
    async def fail(*args):
        raise RuntimeError("db down")

    monkeypatch.setattr(brief, "get_profile", fail)
    monkeypatch.setattr(brief, "get_event_sources", fail)
    steps, result = await run("C-DEV-01", event(1), bank_view=fail)
    limitations = " ".join(result.insight.limitations)
    assert "Профиль компании недоступен" in limitations
    assert "Публикации события недоступны" in limitations
    assert "Позиция банка недоступна" in limitations
    assert result.insight.bank_view.gap
    assert [s.tool_call.ok for s in steps if s.tool_call] == [False, False]
    assert steps[-1].kind == "final"


async def test_run_brief_survives_hanging_reads(monkeypatch):
    async def hang(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(brief, "READ_TIMEOUT_S", 0.05)
    monkeypatch.setattr(brief, "get_profile", hang)
    monkeypatch.setattr(brief, "get_event_sources", hang)
    started = time.perf_counter()
    _, result = await run("C-DEV-01", event(1), bank_view=hang)
    assert time.perf_counter() - started < 1
    assert any("Профиль компании недоступен" in x for x in result.insight.limitations)


class ListStore:
    def __init__(self) -> None:
        self.rows: list[tuple[int, str]] = []

    async def write(self, insight_id, chat_id, rows, *, reset):
        self.rows.extend(rows)


async def test_run_brief_records_context_each_step():
    store = ListStore()
    sink = TraceSink(insight_id=1, store=store)
    steps, result = await run("C-DEV-01", event(1), sink=sink)
    assert len(store.rows) == len(steps) == len(sink.steps)
    records = [TraceRecord.model_validate_json(raw) for _, raw in store.rows]
    assert [r.step for r in records] == steps
    assert all(r.context is not None for r in records)
    assert [e.id for e in records[-1].context.evidence] == [e.id for e in result.evidence]
    # профиль и цепочки появляются в снимке со второго шага
    assert records[0].context.chains == [] and records[1].context.chains
    assert records[-1].context.spent.tool_calls == len([s for s in steps if s.tool_call])


async def test_run_brief_steps_describe_what_happened():
    steps, _ = await run("C-DEV-01", event(1))
    assert steps[0].input == "модель не подключена"
    links = steps[1]
    assert links.kind == "links" and links.output.startswith("4 цепочки: ")
    assert "Ключевая ставка ЦБ × стоимость кредитов (−)" in links.output
    assert [s.kind for s in steps] == ["fallback", "links", "tool", "tool", "final"]
    assert steps[2].tool_call.tool == "get_event_sources"
    assert steps[3].tool_call.args == {"factor": "key_rate", "limit": 3}


async def test_events_fixture_is_not_mutated_by_runs():
    before = [e.model_dump() for e in EVENTS]
    await run("C-DEV-01", event(1))
    assert [e.model_dump() for e in EVENTS] == before


@pytest.mark.parametrize(
    ("n", "word"),
    [(1, "цепочка"), (2, "цепочки"), (5, "цепочек"), (11, "цепочек"), (21, "цепочка"), (12, "цепочек")],
)
def test_chains_word(n, word):
    assert brief._chains_word(n) == word
