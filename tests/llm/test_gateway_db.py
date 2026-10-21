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
        await call()  # ответ резервной из кэша

    rows = db_conn.execute(
        "SELECT model, cached, fallback_from, error IS NOT NULL, event_id, trace_id, cost_rub "
        "FROM llm_calls ORDER BY id"
    ).fetchall()
    assert [r[:6] for r in rows] == [
        (PRIMARY, False, None, True, event_id, rows[0][5]),
        (FALLBACK, False, PRIMARY, False, event_id, rows[0][5]),
        (PRIMARY, False, None, True, event_id, "tr-1"),
        (FALLBACK, True, PRIMARY, False, event_id, "tr-1"),
    ]
    assert rows[3][6] == 0
    assert db_conn.execute("SELECT count(*) FROM llm_cache").fetchone() == (1,)
