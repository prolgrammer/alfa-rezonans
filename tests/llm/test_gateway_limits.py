import asyncio
import logging
import time

import httpx
import pytest
from llm_support import FALLBACK, GOOD, LAST, PRIMARY, Answer, bodies, make_settings, route_by_model
from pydantic import ValidationError

import app.llm.gateway as gateway_module
import app.llm.provider as provider_module
from app.llm.config import load_config
from app.llm.errors import AllModelsFailed, LLMNotConfigured
from app.llm.store import MemoryStore
from app.settings import REPO_ROOT, Settings


async def call(gw, what="привет", **kwargs):
    return await gw.json("event_card", "demo", {"what": what}, Answer, **kwargs)


async def test_repeat_after_fallback_answer_costs_nothing(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: httpx.Response(503, text="down"), FALLBACK: GOOD})
    gw = make_gw()
    first = await call(gw)
    requests_before = len(bodies(route))

    second = await call(gw)

    assert second == first
    assert len(bodies(route)) == requests_before
    last = store.rows[-1]
    assert (last.model, last.cached, last.fallback_from, last.cost_rub) == (FALLBACK, True, PRIMARY, 0)


async def test_slot_timeout_does_not_trip_breaker(api, make_gw, store, monkeypatch):
    # окно ожидания слота: 3,3 с дедлайна минус 3 с запаса
    monkeypatch.setitem(gateway_module.LANE_DEADLINE_S, "background", 3.3)
    route_by_model(api, {PRIMARY: GOOD, FALLBACK: GOOD})
    gw = make_gw(clock=time.monotonic, sleep=asyncio.sleep)
    await call(gw, "прогрев")
    provider = gw._providers["yandex"]
    for _ in range(3):
        await provider._background.acquire()
    for i in range(6):
        with pytest.raises(AllModelsFailed):
            await call(gw, f"v{i}")
    assert gw.breaker.state(PRIMARY) == "closed"
    assert gw.breaker.state(FALLBACK) == "closed"
    assert all(r.error for r in store.rows[-6:])


class SlowCacheStore(MemoryStore):
    """Проверка кэша съедает время полосы: к основной модели остаётся меньше 3 с её половины окна."""

    def __init__(self, clock) -> None:
        super().__init__()
        self._fake_clock = clock

    async def cache_get(self, key):
        self._fake_clock.now += 11.5  # × три модели цепочки event_card
        return await super().cache_get(key)


async def test_deadline_exhaustion_does_not_trip_breaker(api, make_gw, clock):
    route = route_by_model(api, {PRIMARY: GOOD, FALLBACK: GOOD, LAST: GOOD})
    gw = make_gw(store=SlowCacheStore(clock))
    for i in range(6):
        clock.now = 0
        await call(gw, f"v{i}", lane="interactive")
    assert len(bodies(route, PRIMARY)) == 0
    assert gw.breaker.state(PRIMARY) == "closed"
    assert gw.breaker.state(FALLBACK) == "closed"


async def test_non_format_400_does_not_trip_breaker(api, make_gw):
    route_by_model(api, {PRIMARY: httpx.Response(400, text="context too long"), FALLBACK: GOOD})
    gw = make_gw()
    for i in range(6):
        await call(gw, f"v{i}")
    assert gw.breaker.state(PRIMARY) == "closed"


async def test_hanging_provider_fits_lane_deadline(api, make_gw, monkeypatch):
    monkeypatch.setitem(gateway_module.LANE_DEADLINE_S, "background", 1.5)
    monkeypatch.setattr(gateway_module, "MIN_WINDOW_S", 0.1)
    monkeypatch.setattr(provider_module, "MIN_ATTEMPT_WINDOW_S", 0.1)

    async def hang(request):
        await asyncio.sleep(30)

    api.post("/chat/completions").mock(side_effect=hang)
    gw = make_gw(clock=time.monotonic, sleep=asyncio.sleep)
    started = time.monotonic()
    with pytest.raises(AllModelsFailed):
        await call(gw)
    assert time.monotonic() - started < 4


async def test_two_interactive_calls_run_in_parallel_with_idle_background(api, make_gw):
    inside = 0
    both = asyncio.Event()

    async def handler(request):
        nonlocal inside
        inside += 1
        if inside == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)
        return GOOD

    route_by_model(api, {PRIMARY: handler})
    gw = make_gw(clock=time.monotonic, sleep=asyncio.sleep)
    await asyncio.wait_for(
        asyncio.gather(call(gw, "один", lane="interactive"), call(gw, "два", lane="interactive")), 10
    )
    assert both.is_set()


async def test_decoding_error_and_unexpected_error_fall_back(api, make_gw, store):
    def broken(request):
        raise httpx.DecodingError("битое сжатие")

    def crash(request):
        raise RuntimeError("Event loop is closed")

    route_by_model(api, {PRIMARY: broken, FALLBACK: GOOD})
    await call(make_gw())
    assert [r.model for r in store.rows] == [PRIMARY, FALLBACK]

    api.reset()
    route_by_model(api, {PRIMARY: crash, FALLBACK: GOOD})
    await call(make_gw(), "другой")
    assert [r.model for r in store.rows[2:]] == [PRIMARY, FALLBACK]
    assert "Event loop is closed" in store.rows[2].error


async def test_length_finish_reason_is_logged(api, make_gw, caplog):
    caplog.set_level(logging.WARNING)
    body = {
        "choices": [{"message": {"content": '{"value": 1}'}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    route_by_model(api, {PRIMARY: httpx.Response(200, json=body)})
    await call(make_gw())
    assert "оборван по max_tokens" in caplog.text
    assert '"value"' not in caplog.text


async def test_unrelated_format_400_does_not_downgrade_json_mode(api, make_gw):
    route = route_by_model(api, {PRIMARY: httpx.Response(400, text="invalid message format"), FALLBACK: GOOD})
    gw = make_gw()
    await call(gw)
    assert gw._json_mode[PRIMARY] == "schema"
    assert len(bodies(route, PRIMARY)) == 1


async def test_real_json_schema_message_downgrades(api, make_gw):
    route = route_by_model(
        api,
        {PRIMARY: [httpx.Response(400, text="Invalid JSON Schema: all fields must be required"), GOOD]},
    )
    await call(make_gw())
    assert bodies(route, PRIMARY)[1]["response_format"] == {"type": "json_object"}


async def test_interactive_hung_primary_leaves_window_for_fallback(api, make_gw, clock):
    def hung(request):
        clock.now += 30
        return httpx.Response(503, text="down")

    route = route_by_model(api, {PRIMARY: hung, FALLBACK: GOOD})
    answer = await call(make_gw(), lane="interactive")
    assert answer.value == 1
    assert len(bodies(route, PRIMARY)) == 1
    assert len(bodies(route, FALLBACK)) == 1


async def test_missing_settings_is_not_configured(prompts_dir, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)

    def broken_settings():
        return Settings(_env_file=None)

    monkeypatch.setattr(gateway_module, "get_settings", broken_settings)
    gw = gateway_module.Gateway(load_config(REPO_ROOT / "config" / "models.yaml"), prompts_dir=prompts_dir)
    with pytest.raises(LLMNotConfigured):
        await call(gw)
    with pytest.raises(ValidationError):
        broken_settings()


async def test_zero_budget_blocks_calls(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: GOOD})
    from app.llm.errors import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        await call(make_gw(settings=make_settings(llm_daily_budget_rub=0)))
    assert not route.called
    assert store.rows == []
