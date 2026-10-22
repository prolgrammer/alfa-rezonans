import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import pytest
from agent_helpers import sources
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent import trace
from app.agent.context import news_evidence
from app.agent.trace import TraceSink, load_context, load_trace
from app.contracts import AgentStep, StepKind, TraceRecord
from app.db import migrate_up
from app.settings import get_settings

CHAT = "6f1f1d9e-0000-4000-8000-000000000001"


@dataclass
class Call:
    insight_id: int | None
    chat_id: Any
    rows: list[tuple[int, str]]
    reset: bool


class FakeStore:
    def __init__(self, fail: int = 0, hang: bool = False) -> None:
        self.calls: list[Call] = []
        self.fail = fail
        self.hang = hang

    async def write(self, insight_id, chat_id, rows, *, reset):
        self.calls.append(Call(insight_id, chat_id, list(rows), reset))
        if self.hang:
            await asyncio.Event().wait()
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("db down")


async def test_memory_sink_numbers_steps(ctx):
    sink = TraceSink(start=12)
    first = await sink.record(ctx, StepKind.links, "a", "i", "o")
    second = await sink.record(None, StepKind.final, "b", "", "")
    assert (first.seq, second.seq, sink.last_seq) == (13, 14, 14)
    assert sink.steps == [first, second]
    assert await sink.flush() is True


async def test_steps_property_is_a_copy(ctx):
    sink = TraceSink()
    await sink.record(ctx, StepKind.links, "a", "", "")
    sink.steps.clear()
    assert len(sink.steps) == 1


async def test_sink_clips_long_strings(ctx):
    sink = TraceSink()
    step = await sink.record(ctx, StepKind.links, "т" * 300, "в" * 900, "о " * 900)
    assert len(step.title) <= 120 and len(step.input) <= 500 and len(step.output) <= 1000
    assert step.title.endswith("…")
    AgentStep.model_validate(step.model_dump())


async def test_store_failure_defers_and_flush_writes_all(ctx):
    store = FakeStore(fail=1)
    sink = TraceSink(insight_id=7, store=store)
    await sink.record(ctx, StepKind.links, "a", "", "")
    await sink.record(ctx, StepKind.final, "b", "", "")
    assert len(store.calls) == 1
    assert await sink.flush() is True
    assert [seq for seq, _ in store.calls[-1].rows] == [1, 2]
    assert store.calls[-1].reset is True
    assert store.calls[-1].insight_id == 7 and store.calls[-1].chat_id is None


async def test_flush_failure_returns_false_and_logs(ctx, caplog):
    sink = TraceSink(insight_id=7, store=FakeStore(fail=2))
    await sink.record(ctx, StepKind.links, "a", "", "")
    with caplog.at_level(logging.ERROR):
        assert await sink.flush() is False
    assert any(r.levelno == logging.ERROR and "1" in r.getMessage() for r in caplog.records)


async def test_sink_hang_costs_one_timeout(ctx, monkeypatch):
    monkeypatch.setattr(trace, "TRACE_WRITE_TIMEOUT_S", 0.05)
    sink = TraceSink(insight_id=7, store=FakeStore(hang=True))
    started = time.perf_counter()
    for i in range(5):
        await sink.record(ctx, StepKind.tool, f"шаг {i}", "", "")
    assert await sink.flush() is False
    assert time.perf_counter() - started < 0.5
    assert len(sink.steps) == 5


async def test_snapshot_is_taken_at_step_time(ctx):
    store = FakeStore(fail=1)
    sink = TraceSink(insight_id=7, store=store)
    await sink.record(ctx, StepKind.links, "a", "", "")
    news_evidence(ctx, sources(1)[0], "Ставка сохранена.")
    await sink.flush()
    (row,) = store.calls[-1].rows
    saved = TraceRecord.model_validate_json(row[1])
    assert saved.context is not None and saved.context.evidence == []


async def test_reset_only_on_first_successful_write(ctx):
    store = FakeStore()
    sink = TraceSink(insight_id=7, store=store)
    await sink.record(ctx, StepKind.links, "a", "", "")
    await sink.record(ctx, StepKind.final, "b", "", "")
    assert [c.reset for c in store.calls] == [True, False]
    assert [[seq for seq, _ in c.rows] for c in store.calls] == [[1], [2]]


async def test_chat_trace_never_resets():
    store = FakeStore()
    sink = TraceSink(chat_id=CHAT, store=store)
    await sink.record(None, StepKind.plan, "a", "", "")
    assert store.calls[0].reset is False and store.calls[0].insight_id is None


async def test_two_keys_rejected():
    with pytest.raises(ValueError):
        TraceSink(insight_id=1, chat_id=CHAT)


# --- с БД ---------------------------------------------------------------------------------------------


@pytest.fixture
async def db(db_conn, test_db_url, monkeypatch):
    migrate_up(db_conn, get_settings().migrations_dir)
    pool = AsyncConnectionPool(test_db_url, kwargs={"row_factory": dict_row}, open=False)
    await pool.open()

    async def get_pool():
        return pool

    monkeypatch.setattr(trace, "get_pool", get_pool)
    yield pool
    await pool.close()


@pytest.fixture
def insight_id(db_conn, db) -> int:
    event_id = db_conn.execute("INSERT INTO events (title) VALUES ('событие') RETURNING id").fetchone()[0]
    return db_conn.execute(
        "INSERT INTO insights (event_id, company_id, user_role, cache_key) "
        "VALUES (%s, 'C-DEV-01', 'cfo', 'k1') RETURNING id",
        (event_id,),
    ).fetchone()[0]


async def test_pg_roundtrip(db, insight_id, ctx):
    sink = TraceSink(insight_id=insight_id)
    await sink.record(ctx, StepKind.fallback, "a", "i", "o")
    await sink.record(ctx, StepKind.links, "b", "", "")
    news_evidence(ctx, sources(1)[0], "Ставка сохранена.")
    await sink.record(ctx, StepKind.final, "c", "", "", ms=5)
    assert await sink.flush() is True
    assert await load_trace(insight_id) == sink.steps
    assert await load_context(insight_id) == ctx


async def test_load_of_unknown_insight_is_empty(db):
    assert await load_trace(12345) == []
    assert await load_context(12345) is None


async def test_rerun_replaces_trace(db, insight_id, ctx):
    first = TraceSink(insight_id=insight_id)
    for title in ("a", "b", "c"):
        await first.record(ctx, StepKind.tool, title, "", "")
    second = TraceSink(insight_id=insight_id)
    for title in ("x", "y"):
        await second.record(ctx, StepKind.tool, title, "", "")
    assert [s.title for s in await load_trace(insight_id)] == ["x", "y"]


async def test_duplicate_seq_takes_last(db, insight_id, ctx):
    one = await TraceSink().record(ctx, StepKind.tool, "первый", "", "")
    two = one.model_copy(update={"title": "второй"})
    async with db.connection() as conn:
        for step in (one, two):
            await conn.execute(
                "INSERT INTO agent_traces (insight_id, seq, step) VALUES (%s, 1, %s::jsonb)",
                (insight_id, TraceRecord(step=step, context=None).model_dump_json()),
            )
    assert [s.title for s in await load_trace(insight_id)] == ["второй"]


async def test_invalid_row_skipped(db, insight_id, ctx, caplog):
    sink = TraceSink(insight_id=insight_id)
    await sink.record(ctx, StepKind.tool, "годный", "", "")
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_traces (insight_id, seq, step) VALUES (%s, 2, %s::jsonb)",
            (insight_id, '{"step": {"seq": 1}}'),
        )
    with caplog.at_level(logging.WARNING):
        assert [s.title for s in await load_trace(insight_id)] == ["годный"]
    assert any("пропущена" in r.getMessage() for r in caplog.records)


async def test_unknown_insight_fk_does_not_raise(db, ctx):
    sink = TraceSink(insight_id=999)
    step = await sink.record(ctx, StepKind.tool, "a", "", "")
    assert step.seq == 1
    assert await sink.flush() is False
