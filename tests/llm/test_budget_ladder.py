from datetime import datetime

import httpx
import pytest
from llm_support import GOOD, PRIMARY, Answer, make_settings, route_by_model

from app.llm.budget import MSK, Budget, ladder_block, ladder_step
from app.llm.errors import BudgetExceeded, LLMUnavailable, RunLimitExceeded
from app.llm.gateway import llm_scope
from app.llm.store import CallRow

LIMIT = 10.0


def noon() -> datetime:
    return datetime(2026, 10, 21, 12, 0, tzinfo=MSK)


@pytest.mark.parametrize(
    ("spent", "step"),
    [(0, 0), (6.99, 0), (7.0, 1), (8.49, 1), (8.5, 2), (9.5, 3), (9.99, 3), (10.0, 4), (12.0, 4)],
)
def test_ladder_step_thresholds(spent, step):
    assert ladder_step(spent, LIMIT) == step


def test_zero_limit_is_last_step():
    assert ladder_step(0, 0) == 4
    assert ladder_step(0, -1) == 4


@pytest.mark.parametrize(
    ("step", "task", "lane", "priority", "blocked"),
    [
        (0, "insight_write", "background", False, False),
        # 70%: прогрев и массовые прогоны
        (1, "insight_write", "background", False, True),
        (1, "insight_critic", "background", False, True),
        (1, "bank_theses", "background", False, True),
        (1, "eval_judge", "background", False, True),
        (1, "insight_write", "interactive", False, False),
        (1, "event_card", "background", False, False),
        (1, "rank_judge", "background", False, False),
        # 85%: судья, карточки только приоритетные
        (2, "rank_judge", "background", False, True),
        (2, "rank_judge", "interactive", False, True),
        (2, "event_card", "background", False, True),
        (2, "event_card", "background", True, False),
        (2, "event_card", "interactive", False, False),
        (2, "news_filter", "background", False, False),
        (2, "profile_text", "background", False, False),
        # 95%: только интерактив и без критика
        (3, "news_filter", "background", False, True),
        (3, "event_card", "background", True, True),
        (3, "insight_critic", "interactive", False, True),
        (3, "insight_write", "interactive", False, False),
        (3, "chat", "interactive", False, False),
        # 100%: всё
        (4, "chat", "interactive", False, True),
    ],
)
def test_ladder_block(step, task, lane, priority, blocked):
    assert (ladder_block(step, task, lane, priority=priority) is not None) is blocked


def test_budget_check_raises_with_step():
    budget = Budget(LIMIT, now=noon)
    budget.add(8.6)
    with pytest.raises(BudgetExceeded) as caught:
        budget.check("rank_judge", "background", False)
    assert caught.value.step == 2
    assert "судья" in str(caught.value)
    budget.check("rank_judge", "background", True)  # essential проходит


def test_step_ignores_holds():
    budget = Budget(LIMIT, now=noon)
    budget.add(6.0)
    with budget.hold("t", False, 2.0):
        assert budget.step == 0


def test_state():
    budget = Budget(LIMIT, now=noon)
    budget.add(7.25)
    state = budget.state()
    assert (state.day.isoformat(), state.limit_rub, state.spent_rub, state.step) == (
        "2026-10-21",
        LIMIT,
        7.25,
        1,
    )
    assert state.since.isoformat() == "2026-10-20T21:00:00+00:00"


async def spend(store, rub: float) -> None:
    await store.record(CallRow(task="event_card", model=PRIMARY, lane="background", cost_rub=rub))


async def call(gw, task="event_card", what="привет", **kwargs):
    return await gw.json(task, "demo", {"what": what}, Answer, **kwargs)


# в шлюзе лимит больше: резерв insight_write — около 3 ₽, он не должен упираться в сам лимит
GW_LIMIT = 100.0


def gw_at(make_gw):
    return make_gw(settings=make_settings(llm_daily_budget_rub=GW_LIMIT))


async def test_warm_stops_at_70(api, make_gw, store):
    route = route_by_model(api, {"glm-5.3-flash": GOOD})
    await spend(store, 75)
    gw = gw_at(make_gw)
    with pytest.raises(BudgetExceeded) as caught:
        await call(gw, "insight_write")
    assert caught.value.step == 1
    assert isinstance(caught.value, LLMUnavailable)
    assert not route.called
    # интерактивный разбор по клику проходит
    assert (await call(gw, "insight_write", lane="interactive")).value == 1


async def test_cards_at_85_only_priority(api, make_gw, store):
    route_by_model(api, {PRIMARY: GOOD})
    await spend(store, 86)
    gw = gw_at(make_gw)
    with pytest.raises(BudgetExceeded):
        await call(gw)
    assert (await call(gw, priority=True)).value == 1


async def test_critic_off_at_95(api, make_gw, store):
    route_by_model(api, {"deepseek-v4.1-flash": GOOD, "glm-5.3-flash": GOOD})
    await spend(store, 96)
    gw = gw_at(make_gw)
    with pytest.raises(BudgetExceeded):
        await call(gw, "insight_critic", lane="interactive")
    assert (await call(gw, "insight_write", lane="interactive")).value == 1


async def test_cache_served_on_any_step(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: GOOD})
    gw = gw_at(make_gw)
    assert (await call(gw)).value == 1
    await spend(store, GW_LIMIT)
    gw_full = gw_at(make_gw)
    assert (await call(gw_full)).value == 1
    assert route.call_count == 1
    with pytest.raises(BudgetExceeded):
        await gw_full.json("event_card", "demo", {"what": "другое"}, Answer)


async def test_budget_state(make_gw, store):
    await spend(store, 90)
    state = await gw_at(make_gw).budget_state()
    assert (state.limit_rub, state.spent_rub, state.step) == (GW_LIMIT, 90, 2)


async def test_run_limit_zero_refuses_at_once(api, make_gw):
    route = route_by_model(api, {PRIMARY: GOOD})
    with llm_scope(limit_rub=0.0) as scope, pytest.raises(RunLimitExceeded):
        await call(make_gw())
    assert not route.called
    assert scope.held == 0
    assert isinstance(RunLimitExceeded("x"), BudgetExceeded)


async def test_run_limit_counts_spent(api, make_gw):
    route_by_model(api, {PRIMARY: httpx.Response(200, json=GOOD_BIG)})
    gw = make_gw()
    with llm_scope(limit_rub=0.5) as scope:
        await call(gw, what="раз")  # перерасход — не больше одного вызова
        assert scope.cost_rub > 0.5
        with pytest.raises(RunLimitExceeded):
            await call(gw, what="два")
        assert scope.held == 0


async def test_run_limit_with_real_prices_lets_insight_through(api, make_gw):
    # резерв шага на GLM (~3,6 ₽ сверху) не должен сам по себе упираться в INSIGHT_MAX_RUB
    route_by_model(api, {"glm-5.3-flash": GOOD})
    gw = make_gw()
    limit = make_settings().insight_max_rub
    with llm_scope(limit_rub=limit) as scope:
        await call(gw, "insight_plan", what="план " * 800, lane="interactive")
        await call(gw, "insight_write", what="черновик " * 800, lane="interactive")
    assert 0 < scope.cost_rub < limit


async def test_run_limit_sees_parallel_holds(api, make_gw):
    route = route_by_model(api, {PRIMARY: GOOD})
    with llm_scope(limit_rub=4.0) as scope:
        scope.held = 4.0  # параллельные вызовы области уже держат весь лимит
        with pytest.raises(RunLimitExceeded):
            await call(make_gw())
    assert not route.called


async def test_run_limit_stops_before_fallback(api, make_gw):
    # основная модель дважды ответила мусором и съела лимит: резервную уже не зовём
    broken = {**GOOD_BIG, "choices": [{"message": {"content": "{не json"}, "finish_reason": "stop"}]}
    route = route_by_model(api, {PRIMARY: httpx.Response(200, json=broken), "glm-5.3-flash": GOOD})
    with llm_scope(limit_rub=1.0) as scope, pytest.raises(RunLimitExceeded):
        await call(make_gw())
    assert route.call_count == 2
    assert scope.cost_rub > 1.0


async def test_run_limit_allows_cache(api, make_gw):
    route = route_by_model(api, {PRIMARY: GOOD})
    gw = make_gw()
    await call(gw)
    with llm_scope(limit_rub=0.0):
        assert (await call(gw)).value == 1
    assert route.call_count == 1


async def test_scope_without_limit_unchanged(api, make_gw):
    route_by_model(api, {PRIMARY: GOOD})
    with llm_scope() as scope:
        await call(make_gw())
    assert scope.limit_rub is None and scope.held == 0


async def test_probe_obeys_ladder(make_gw, store):
    await spend(store, 96)
    with pytest.raises(BudgetExceeded) as caught:
        await gw_at(make_gw).raw_call(PRIMARY, [{"role": "user", "content": "x"}])
    assert caught.value.step == 3


# ответ, после которого в лимите 1 ₽ места под второй вызов уже нет
GOOD_BIG = {
    "choices": [{"message": {"content": '{"value": 1}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 1500},
}
