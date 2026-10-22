import asyncio

import pytest

from app.agent import guard
from app.agent.guard import bounded


async def test_bounded_returns_value():
    assert await bounded(asyncio.sleep(0, "x"), 1) == "x"


async def test_bounded_timeout_keeps_task_alive():
    gate = asyncio.Event()
    finished = []

    async def work():
        await gate.wait()
        finished.append("done")
        return 1

    with pytest.raises(TimeoutError):
        await bounded(work(), 0.01)
    assert len(guard._background) == 1
    (task,) = guard._background
    assert not task.done() and not task.cancelled()
    gate.set()
    await asyncio.wait_for(asyncio.shield(task), 1)
    await asyncio.sleep(0)
    assert finished == ["done"]
    assert not guard._background


async def test_bounded_propagates_error():
    async def fail():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await bounded(fail(), 1)
    assert not guard._background


async def test_late_error_is_logged_not_lost(caplog):
    gate = asyncio.Event()

    async def fail():
        await gate.wait()
        raise RuntimeError("late")

    with pytest.raises(TimeoutError):
        await bounded(fail(), 0.01)
    gate.set()
    for _ in range(3):
        await asyncio.sleep(0)
    assert any("после таймаута" in r.getMessage() for r in caplog.records)
    assert not guard._background


async def test_cancelled_caller_does_not_cancel_work():
    gate = asyncio.Event()
    state = []

    async def work():
        await gate.wait()
        state.append("done")

    waiter = asyncio.ensure_future(bounded(work(), 5))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.set()
    for _ in range(3):
        await asyncio.sleep(0)
    assert state == ["done"]
