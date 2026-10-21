import logging

import httpx
import pytest
from llm_support import (
    FALLBACK,
    FOLDER,
    GOOD,
    KEY,
    PRIMARY,
    Answer,
    bodies,
    completion,
    make_settings,
    route_by_model,
)

from app.llm.config import load_config
from app.llm.errors import AllModelsFailed, BudgetExceeded, LLMNotConfigured
from app.llm.gateway import Breaker, llm_scope
from app.settings import REPO_ROOT

ASK = {"what": "привет"}


async def call(gw, what="привет", **kwargs):
    return await gw.json("event_card", "demo", {"what": what}, Answer, **kwargs)


async def test_ok_records_call(api, make_gw, store):
    route_by_model(api, {PRIMARY: GOOD})
    answer = await call(make_gw())
    assert answer == Answer(value=1, note="ok")
    (row,) = store.rows
    assert (row.task, row.model, row.prompt_version, row.cached, row.lane) == (
        "event_card",
        PRIMARY,
        "demo@1",
        False,
        "background",
    )
    assert row.cost_rub == round(10 * 0.20 / 1000 + 5 * 0.30 / 1000, 4)
    assert (row.tokens_in, row.tokens_out, row.fallback_from, row.error) == (10, 5, None, None)


async def test_prompt_text_reaches_model(api, make_gw):
    route = route_by_model(api, {PRIMARY: GOOD})
    await call(make_gw(), "мир")
    (body,) = bodies(route)
    assert body["messages"][0] == {"role": "user", "content": "Скажи мир"}
    assert body["model"] == f"gpt://{FOLDER}/{PRIMARY}/latest"
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 1500


async def test_fallback_on_503(api, make_gw, store):
    route_by_model(api, {PRIMARY: httpx.Response(503, text="overloaded"), FALLBACK: GOOD})
    await call(make_gw())
    first, second = store.rows
    assert (first.model, first.fallback_from) == (PRIMARY, None)
    assert first.error
    assert (second.model, second.fallback_from, second.error) == (FALLBACK, PRIMARY, None)


async def test_fallback_on_timeout(api, make_gw, store):
    route_by_model(
        api, {PRIMARY: lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("медленно")), FALLBACK: GOOD}
    )
    await call(make_gw())
    assert [r.model for r in store.rows] == [PRIMARY, FALLBACK]


async def test_fallback_on_invalid_json_twice(api, make_gw, store):
    bad = completion('{"value": "не число"}')
    route = route_by_model(api, {PRIMARY: bad, FALLBACK: GOOD})
    await call(make_gw())
    assert len(bodies(route, PRIMARY)) == 2
    first, second = store.rows
    assert (first.tokens_in, first.tokens_out) == (20, 10)
    assert "не прошёл проверку" in first.error
    assert second.model == FALLBACK


async def test_validation_retry_same_model(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: [completion('{"value": "x"}'), GOOD]})
    answer = await call(make_gw())
    assert answer.value == 1
    sent = bodies(route, PRIMARY)
    assert len(sent) == 2
    assert [m["role"] for m in sent[1]["messages"]] == ["user", "assistant", "user"]
    assert "Исправь" in sent[1]["messages"][2]["content"]
    (row,) = store.rows
    assert (row.tokens_in, row.tokens_out) == (20, 10)


async def test_refusal_without_brace_no_retry(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: completion("Не могу ответить"), FALLBACK: GOOD})
    await call(make_gw())
    assert len(bodies(route, PRIMARY)) == 1
    assert [r.model for r in store.rows] == [PRIMARY, FALLBACK]


async def test_json_mode_downgrade_remembered(api, make_gw):
    route = route_by_model(
        api, {PRIMARY: [httpx.Response(400, text="Invalid response_format: json_schema"), GOOD]}
    )
    gw = make_gw()
    await call(gw, "один")
    first, second = bodies(route, PRIMARY)
    assert first["response_format"]["type"] == "json_schema"
    assert second["response_format"] == {"type": "json_object"}
    assert "JSON-схеме" in second["messages"][0]["content"]

    await call(gw, "два")
    third = bodies(route, PRIMARY)[2]
    assert third["response_format"] == {"type": "json_object"}


async def test_json_mode_downgrades_down_to_prompt(api, make_gw):
    route = route_by_model(
        api,
        {
            PRIMARY: [
                httpx.Response(400, text="bad response_format"),
                httpx.Response(400, text="json_object is not supported"),
                GOOD,
            ]
        },
    )
    await call(make_gw())
    modes = [b.get("response_format", {}).get("type") for b in bodies(route, PRIMARY)]
    assert modes == ["json_schema", "json_object", None]
    assert "JSON-схеме" in bodies(route, PRIMARY)[2]["messages"][0]["content"]


async def test_unrelated_400_is_failure(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: httpx.Response(400, text="context too long"), FALLBACK: GOOD})
    await call(make_gw())
    assert len(bodies(route, PRIMARY)) == 1
    assert store.rows[0].error


async def test_strict_schema_sent(api, make_gw):
    route = route_by_model(api, {PRIMARY: GOOD})
    await call(make_gw())
    schema = bodies(route)[0]["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == {"value", "note"}


async def test_null_of_optional_field_gives_default(api, make_gw):
    route_by_model(api, {PRIMARY: completion('{"value": 5, "note": null}')})
    assert await call(make_gw()) == Answer(value=5)


async def test_reasoning_effort_off_for_qwen(api, make_gw):
    route = route_by_model(api, {PRIMARY: GOOD, "glm-5.3-flash": GOOD})
    gw = make_gw()
    await call(gw)
    assert bodies(route)[0]["reasoning_effort"] == "none"
    # задача с рассуждениями: параметр не шлём
    await gw.json("insight_plan", "demo", ASK, Answer)
    assert "reasoning_effort" not in bodies(route)[1]


async def test_reasoning_param_dropped_on_400(api, make_gw):
    route = route_by_model(api, {PRIMARY: [httpx.Response(400, text="unknown field reasoning_effort"), GOOD]})
    gw = make_gw()
    await call(gw, "один")
    first, second = bodies(route)
    assert "reasoning_effort" in first
    assert "reasoning_effort" not in second
    await call(gw, "два")
    assert "reasoning_effort" not in bodies(route)[2]


async def test_breaker_skips_then_probes(api, make_gw, clock):
    failing = httpx.Response(503, text="down")
    route = route_by_model(api, {PRIMARY: [failing] * 15 + [GOOD], FALLBACK: GOOD})
    gw = make_gw()
    for i in range(5):
        await call(gw, f"v{i}")
    assert len(bodies(route, PRIMARY)) == 15
    assert gw.breaker.state(PRIMARY) == "open"

    await call(gw, "пропуск")
    assert len(bodies(route, PRIMARY)) == 15

    clock.now += 61
    assert gw.breaker.state(PRIMARY) == "half_open"
    await call(gw, "проба")
    assert len(bodies(route, PRIMARY)) == 16
    assert gw.breaker.state(PRIMARY) == "closed"


async def test_breaker_reopens_after_failed_probe(api, make_gw, clock):
    route = route_by_model(api, {PRIMARY: httpx.Response(503, text="down"), FALLBACK: GOOD})
    gw = make_gw()
    for i in range(5):
        await call(gw, f"v{i}")
    clock.now += 61
    await call(gw, "проба")
    assert gw.breaker.state(PRIMARY) == "open"
    before = len(bodies(route, PRIMARY))
    await call(gw, "после пробы")
    assert len(bodies(route, PRIMARY)) == before


async def test_breaker_still_tries_last_model(api, make_gw):
    route = route_by_model(
        api, {PRIMARY: httpx.Response(503, text="down"), FALLBACK: httpx.Response(503, text="down")}
    )
    gw = make_gw()
    for i in range(5):
        with pytest.raises(AllModelsFailed):
            await call(gw, f"v{i}")
    assert gw.breaker.state(FALLBACK) == "open"
    before = len(bodies(route, FALLBACK))
    with pytest.raises(AllModelsFailed):
        await call(gw, "ещё")
    assert len(bodies(route, FALLBACK)) > before


def test_breaker_unit():
    now = [0.0]
    breaker = Breaker(threshold=2, cooldown_s=60, clock=lambda: now[0])
    breaker.failure("m")
    assert breaker.allows("m")
    breaker.failure("m")
    assert breaker.state("m") == "open" and not breaker.allows("m")
    now[0] = 61
    assert breaker.state("m") == "half_open" and breaker.allows("m")
    breaker.success("m")
    assert breaker.state("m") == "closed"


async def test_cache_hit_free(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: GOOD})
    gw = make_gw()
    first = await call(gw)
    second = await call(gw)
    assert first == second
    assert len(bodies(route)) == 1
    assert [r.cached for r in store.rows] == [False, True]
    assert store.rows[1].cost_rub == 0
    assert store.rows[1].prompt_version == "demo@1"


async def test_cache_miss_on_other_prompt_version(api, make_gw, prompts_dir):
    route = route_by_model(api, {PRIMARY: GOOD})
    gw = make_gw()
    await call(gw)
    (prompts_dir / "demo.md").write_text(
        "---\ntask: event_card\nversion: 2\n---\nСкажи {{ what }}", encoding="utf-8"
    )
    await call(gw)
    assert len(bodies(route)) == 2


async def test_stale_cache_ignored(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: GOOD})
    gw = make_gw()
    await call(gw)
    (key,) = store._cache
    store._cache[key] = {"value": "устаревшая схема"}
    assert await call(gw) == Answer(value=1, note="ok")
    assert len(bodies(route)) == 2


async def test_budget_exceeded(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: GOOD})
    gw = make_gw(settings=make_settings(llm_daily_budget_rub=0.0001))
    with pytest.raises(BudgetExceeded):
        await call(gw)
    assert not route.called
    assert store.rows == []


async def test_not_configured(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: GOOD})
    for settings in (make_settings(llm_api_key=""), make_settings(llm_folder_id="")):
        with pytest.raises(LLMNotConfigured):
            await call(make_gw(settings=settings))
    assert not route.called
    assert store.rows == []


async def test_unknown_task_keyerror(make_gw):
    with pytest.raises(KeyError):
        await make_gw().json("nope", "demo", ASK, Answer)


async def test_model_outside_whitelist_rejected(make_gw):
    gw = make_gw()
    gw.config = load_config(REPO_ROOT / "config" / "models.yaml")
    task = gw.config.tasks["event_card"]
    gw.config.tasks["event_card"] = task.model_copy(update={"model": "gpt-4o"})
    with pytest.raises(ValueError, match="gpt-4o"):
        await call(gw)


async def test_all_down_fails_fast(api, make_gw, clock):
    route = api.post("/chat/completions").mock(side_effect=httpx.ConnectError("нет связи"))
    with pytest.raises(AllModelsFailed):
        await call(make_gw())
    assert route.call_count == 6
    assert clock.now <= 180


async def test_key_never_leaks(api, make_gw, store, caplog):
    caplog.set_level(logging.DEBUG)
    leaky = f"denied for Api-Key {KEY} in folder {FOLDER} gpt://{FOLDER}/m/latest"
    route_by_model(api, {PRIMARY: httpx.Response(401, text=leaky), FALLBACK: httpx.Response(500, text=leaky)})
    with pytest.raises(AllModelsFailed) as info:
        await call(make_gw())
    visible = [caplog.text, str(info.value), repr(info.value), *(r.error or "" for r in store.rows)]
    for text in visible:
        assert KEY not in text
        assert FOLDER not in text
    assert all(r.error for r in store.rows)


async def test_scope_collects_and_tags(api, make_gw, store):
    route_by_model(api, {PRIMARY: GOOD})
    gw = make_gw()
    with llm_scope(trace_id="t1", event_id=7) as scope:
        await call(gw, "один")
        await call(gw, "два")
        await call(gw, "три", event_id=9)
    assert [(r.trace_id, r.event_id) for r in store.rows] == [("t1", 7), ("t1", 7), ("t1", 9)]
    assert scope.cost_rub == pytest.approx(sum(r.cost_rub for r in store.rows))
    assert (scope.tokens_in, scope.tokens_out) == (30, 15)
    assert [c.model for c in scope.calls] == [PRIMARY] * 3

    await call(gw, "вне области")
    assert store.rows[-1].trace_id and store.rows[-1].trace_id != "t1"
    assert store.rows[-1].event_id is None


async def test_lane_deadline_interactive(api, make_gw, clock):
    starts: list[float] = []

    def slow_failure(request):
        starts.append(clock.now)
        clock.now += 20
        return httpx.Response(503, text="down")

    route_by_model(api, {PRIMARY: slow_failure, FALLBACK: slow_failure})
    with pytest.raises(AllModelsFailed):
        await call(make_gw(), lane="interactive")
    assert starts and max(starts) < 37

    starts.clear()
    clock.now = 0
    with pytest.raises(AllModelsFailed):
        await call(make_gw(), "фон", lane="background")
    assert max(starts) > 37


async def test_interactive_lane_passes_when_background_busy(api, make_gw, store):
    route_by_model(api, {PRIMARY: GOOD})
    gw = make_gw()
    await call(gw, "прогрев")
    background = gw._providers["yandex"].lane("background")
    for _ in range(3):
        await background.acquire()
    await call(gw, "разбор по клику", lane="interactive")
    assert store.rows[-1].lane == "interactive"
