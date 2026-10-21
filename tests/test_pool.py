import asyncio

from app import db


class FakePool:
    created = 0

    def __init__(self, *args, **kwargs):
        type(self).created += 1

    async def open(self, **kwargs):
        await asyncio.sleep(0.05)

    async def close(self):
        pass


async def test_concurrent_first_calls_create_one_pool(monkeypatch):
    monkeypatch.setattr(db, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "_pool_lock", asyncio.Lock())
    first, second = await asyncio.gather(db.get_pool(), db.get_pool())
    assert first is second
    assert FakePool.created == 1
    await db.close_pool()
