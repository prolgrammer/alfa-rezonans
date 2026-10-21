import httpx
from llm_support import FALLBACK, GOOD, PRIMARY, Answer, route_by_model

from app.llm.gateway import llm_scope
from app.llm.store import PgStore


async def test_calls_written_for_success_failure_and_cache(api, make_gw, pg_pool, db_conn):
    event_id = db_conn.execute("INSERT INTO events (title) VALUES ('событие') RETURNING id").fetchone()[0]
    route_by_model(api, {PRIMARY: httpx.Response(503, text="down"), FALLBACK: GOOD})
    gw = make_gw(store=PgStore(pg_pool))

    async def call(**kwargs):
        return await gw.json("event_card", "demo", {"what": "привет"}, Answer, **kwargs)

    await call(event_id=event_id)  # неудача основной и успех резервной
    with llm_scope(trace_id="tr-1", event_id=event_id):
        await call()  # повтор целиком из кэша резервной, основная не вызывается

    rows = db_conn.execute(
        "SELECT model, cached, fallback_from, error IS NOT NULL, event_id, trace_id, cost_rub "
        "FROM llm_calls ORDER BY id"
    ).fetchall()
    assert [r[:6] for r in rows] == [
        (PRIMARY, False, None, True, event_id, rows[0][5]),
        (FALLBACK, False, PRIMARY, False, event_id, rows[0][5]),
        (FALLBACK, True, PRIMARY, False, event_id, "tr-1"),
    ]
    assert rows[2][6] == 0
    assert db_conn.execute("SELECT count(*) FROM llm_cache").fetchone() == (1,)


async def test_usage_since_groups_by_task_and_model(pg_pool, db_conn):
    from datetime import UTC, datetime, timedelta

    from app.llm.store import usage_since

    rows = [
        # задача, модель, ₽, мс, из кэша, резерв, ошибка, сутки назад
        ("event_card", PRIMARY, 0.5, 100, False, None, None, False),
        ("event_card", PRIMARY, 0.7, 300, False, None, "сбой", False),
        ("event_card", PRIMARY, 0.0, 0, True, None, None, False),
        ("event_card", FALLBACK, 0.4, 900, False, PRIMARY, None, False),
        # повтор из кэша резервной модели — не переключение и не задержка модели
        ("event_card", FALLBACK, 0.0, 0, True, PRIMARY, None, False),
        ("chat", PRIMARY, 2.0, 500, False, None, None, False),
        ("chat", PRIMARY, 9.0, 500, False, None, None, True),
    ]
    now = datetime.now(UTC)
    for task, model, rub, ms, cached, fallback, error, old in rows:
        db_conn.execute(
            "INSERT INTO llm_calls (task, model, cost_rub, ms, cached, fallback_from, error, lane, "
            "created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'background', %s)",
            (task, model, rub, ms, cached, fallback, error, now - timedelta(days=1) if old else now),
        )
    usage = await usage_since(pg_pool, now - timedelta(hours=1))
    assert usage == [
        {
            "task": "chat",
            "model": PRIMARY,
            "calls": 1,
            "cost_rub": 2.0,
            "fallbacks": 0,
            "errors": 0,
            "p50_ms": 500,
        },
        {
            "task": "event_card",
            "model": PRIMARY,
            "calls": 3,
            "cost_rub": 1.2,
            "fallbacks": 0,
            "errors": 1,
            "p50_ms": 200,
        },
        {
            "task": "event_card",
            "model": FALLBACK,
            "calls": 2,
            "cost_rub": 0.4,
            "fallbacks": 1,
            "errors": 0,
            "p50_ms": 900,
        },
    ]


async def test_usage_since_empty_day(pg_pool):
    from datetime import UTC, datetime

    from app.llm.store import usage_since

    assert await usage_since(pg_pool, datetime.now(UTC)) == []
