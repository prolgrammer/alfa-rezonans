import logging
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.db import migrate_up
from app.llm.store import CallRow, MemoryStore, PgStore
from app.settings import get_settings


async def test_memory_store_roundtrip():
    store = MemoryStore()
    assert await store.cache_get("k") is None
    await store.cache_put("k", "task", "model", {"a": 1})
    assert await store.cache_get("k") == {"a": 1}

    await store.record(CallRow(task="t", model="m", lane="background", cost_rub=1.5))
    await store.record(CallRow(task="t", model="m", lane="background", cost_rub=0.5))
    assert len(store.rows) == 2
    assert await store.spent_since(datetime.now(UTC) - timedelta(minutes=1)) == 2.0
    assert await store.spent_since(datetime.now(UTC) + timedelta(minutes=1)) == 0.0


@pytest.fixture
async def pg_pool(test_db_url, db_conn):
    migrate_up(db_conn, get_settings().migrations_dir)
    pool = AsyncConnectionPool(test_db_url, kwargs={"row_factory": dict_row}, open=False)
    await pool.open()
    yield pool
    await pool.close()


async def test_pg_store_writes_calls_and_cache(pg_pool, db_conn):
    store = PgStore(pg_pool)
    start = datetime.now(UTC) - timedelta(minutes=1)
    await store.record(
        CallRow(
            task="event_card",
            model="qwen3.6-35b-a3b",
            lane="interactive",
            prompt_version="event_card@3",
            tokens_in=10,
            tokens_out=5,
            cost_rub=0.0035,
            ms=120,
            cached=False,
            fallback_from="glm-5.3-flash",
            error="x" * 600,
            trace_id="tr-1",
        )
    )
    await store.record(CallRow(task="event_card", model="m", lane="background", cached=True))

    rows = db_conn.execute(
        "SELECT task, cached, fallback_from, event_id, trace_id, length(error), prompt_version "
        "FROM llm_calls ORDER BY id"
    ).fetchall()
    assert rows == [
        ("event_card", False, "glm-5.3-flash", None, "tr-1", 500, "event_card@3"),
        ("event_card", True, None, None, None, None, None),
    ]
    assert await store.spent_since(start) == pytest.approx(0.0035)

    assert await store.cache_get("key") is None
    await store.cache_put("key", "event_card", "m", {"v": 1})
    await store.cache_put("key", "event_card", "m", {"v": 2})
    assert await store.cache_get("key") == {"v": 2}
    assert db_conn.execute("SELECT count(*) FROM llm_cache").fetchone() == (1,)


async def test_pg_store_writes_event_id(pg_pool, db_conn):
    event_id = db_conn.execute("INSERT INTO events (title) VALUES ('событие') RETURNING id").fetchone()[0]
    await PgStore(pg_pool).record(CallRow(task="t", model="m", lane="background", event_id=event_id))
    assert db_conn.execute("SELECT event_id FROM llm_calls").fetchone() == (event_id,)


async def test_pg_store_survives_db_down(caplog):
    pool = AsyncConnectionPool(
        "postgresql://x:secret-pass@127.0.0.1:1/orientir_test", kwargs={"row_factory": dict_row}, open=False
    )
    store = PgStore(pool)
    with caplog.at_level(logging.WARNING):
        await store.record(CallRow(task="t", model="m", lane="background"))
        assert await store.cache_get("k") is None
        await store.cache_put("k", "t", "m", {})
        assert await store.spent_since(datetime.now(UTC)) is None
    assert "БД недоступна" in caplog.text
    assert "secret-pass" not in caplog.text
