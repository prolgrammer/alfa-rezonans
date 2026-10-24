import pytest
from agent_helpers import session

from app.agent import run
from app.agent.run import run_insight
from app.bank.theses import fixture_bank_view
from app.contracts import AgentStep, InsightResult
from app.fixtures import load_fixture
from app.llm import BudgetExceeded
from app.llm.gateway import gw


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(run, "STEP_DELAY_S", (0, 0))
    monkeypatch.setattr(run, "_bank_view", fixture_bank_view)


async def collect(company: str, event_id: int, **kwargs):
    items = [x async for x in run_insight(session(company), event_id, **kwargs)]
    *steps, last = items
    assert all(isinstance(s, AgentStep) for s in steps)
    return steps, last


async def test_no_key_gives_brief():
    steps, last = await collect("C-DEV-01", 1)
    assert isinstance(last, InsightResult) and last.mode == "brief"
    assert steps[0].kind == "fallback" and steps[0].input == "модель не подключена"
    assert [s.seq for s in steps] == list(range(1, len(steps) + 1))
    assert last.insight.limitations[0].startswith("Краткий разбор: модель не подключена")


async def test_configured_gives_stub(monkeypatch):
    monkeypatch.setattr(gw, "check_configured", lambda: None)
    steps, last = await collect("C-DEV-01", 1)
    assert last.mode == "full"
    assert steps == load_fixture("trace.json", run._TRACE)


async def test_failure_mid_stream_continues_numbering(monkeypatch):
    fixture_steps = load_fixture("trace.json", run._TRACE)[:2]

    async def broken(ctx, event):
        for step in fixture_steps:
            yield step
        raise BudgetExceeded("лимит")

    monkeypatch.setattr(run, "_run_full", broken)
    steps, last = await collect("C-DEV-01", 1)
    assert steps[:2] == fixture_steps
    assert steps[2].kind == "fallback" and steps[2].seq == 3
    assert [s.seq for s in steps] == list(range(1, len(steps) + 1))
    assert last.mode == "brief"
    assert last.insight.limitations[0].startswith("Краткий разбор: исчерпан дневной бюджет на модели")


async def test_unknown_event_fails_before_first_step():
    with pytest.raises(LookupError):
        await anext(run_insight(session("C-DEV-01"), 999))


async def test_insight_id_is_optional_keyword_and_trace_is_written(monkeypatch):
    written = []

    class Store:
        async def write(self, insight_id, chat_id, rows, *, reset):
            written.append((insight_id, [seq for seq, _ in rows], reset))

    monkeypatch.setattr("app.agent.trace.PgTraceStore", Store)
    steps, _ = await collect("C-IT-01", 1, insight_id=42)
    assert [call[0] for call in written] == [42] * len(steps)
    assert written[0][2] is True and not any(call[2] for call in written[1:])
    assert [call[1][0] for call in written] == [s.seq for s in steps]
