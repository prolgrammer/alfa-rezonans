from datetime import datetime

import pytest

from app.llm.budget import MSK, Budget
from app.llm.errors import BudgetExceeded
from app.llm.store import CallRow, MemoryStore


def noon(day: int = 21) -> datetime:
    return datetime(2026, 10, day, 12, 0, tzinfo=MSK)


def test_hold_rejects_over_limit():
    budget = Budget(1.0, now=noon)
    budget.add(0.9)
    with pytest.raises(BudgetExceeded), budget.hold("t", False, 0.2):
        pass
    with budget.hold("t", False, 0.1):
        pass


def test_parallel_holds_see_each_other():
    budget = Budget(1.0, now=noon)
    with budget.hold("a", False, 0.6):
        with pytest.raises(BudgetExceeded), budget.hold("b", False, 0.6):
            pass
    assert budget.held == 0
    with budget.hold("b", False, 0.6):
        pass


def test_essential_ignores_limit():
    budget = Budget(1.0, now=noon)
    budget.add(5.0)
    with budget.hold("t", True, 3.0):
        assert budget.held == 3.0


def test_zero_limit_means_unlimited():
    budget = Budget(0, now=noon)
    budget.add(1000)
    with budget.hold("t", False, 1000):
        pass


def test_day_rollover_resets():
    current = [noon(21)]
    budget = Budget(1.0, now=lambda: current[0])
    budget.add(0.95)
    current[0] = noon(22)
    with budget.hold("t", False, 0.5):
        pass
    assert budget.spent == 0


class CountingStore(MemoryStore):
    reads = 0

    async def spent_since(self, start):
        type(self).reads += 1
        return await super().spent_since(start)


async def test_sync_takes_max_and_throttles():
    now = [0.0]
    store = CountingStore()
    budget = Budget(10.0, store=store, clock=lambda: now[0])
    await store.record(CallRow(task="t", model="m", lane="background", cost_rub=2.0))

    await budget.sync()
    assert budget.spent == 2.0
    assert CountingStore.reads == 1

    budget.add(1.0)
    await store.record(CallRow(task="t", model="m", lane="background", cost_rub=1.0))
    now[0] = 30.0
    await budget.sync()
    assert CountingStore.reads == 1

    now[0] = 61.0
    await budget.sync()
    assert CountingStore.reads == 2
    assert budget.spent == 3.0


async def test_sync_keeps_memory_when_store_unavailable():
    class Down(MemoryStore):
        async def spent_since(self, start):
            return None

    budget = Budget(10.0, store=Down())
    budget.add(4.0)
    await budget.sync()
    assert budget.spent == 4.0
