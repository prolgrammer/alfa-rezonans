import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)

DB_TIMEOUT_S = 3.0
ERROR_LIMIT = 500


@dataclass
class CallRow:
    task: str
    model: str
    lane: str
    prompt_version: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_rub: float = 0.0
    ms: int = 0
    cached: bool = False
    fallback_from: str | None = None
    error: str | None = None
    trace_id: str | None = None
    event_id: int | None = None


class Store(Protocol):
    async def cache_get(self, key: str) -> Any | None: ...

    async def cache_put(self, key: str, task: str, model: str, response: Any) -> None: ...

    async def record(self, row: CallRow) -> None: ...

    # None: источник недоступен, и сверять бюджет не с чем
    async def spent_since(self, start: datetime) -> float | None: ...


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[CallRow] = []
        self._times: list[datetime] = []
        self._cache: dict[str, Any] = {}

    async def cache_get(self, key: str) -> Any | None:
        return self._cache.get(key)

    async def cache_put(self, key: str, task: str, model: str, response: Any) -> None:
        self._cache[key] = response

    async def record(self, row: CallRow) -> None:
        self.rows.append(row)
        self._times.append(datetime.now(UTC))

    async def spent_since(self, start: datetime) -> float | None:
        return sum(r.cost_rub for r, at in zip(self.rows, self._times, strict=True) if at >= start)


class PgStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    def _fail(self, operation: str, exc: Exception) -> None:
        # без текста запроса и данных: в них могут оказаться чужие секреты
        log.warning("шлюз: БД недоступна при операции %s (%s)", operation, type(exc).__name__)

    async def cache_get(self, key: str) -> Any | None:
        try:
            async with asyncio.timeout(DB_TIMEOUT_S), self.pool.connection(timeout=DB_TIMEOUT_S) as conn:
                cur = await conn.execute("SELECT response FROM llm_cache WHERE key = %s", (key,))
                row = await cur.fetchone()
        except Exception as exc:
            self._fail("cache_get", exc)
            return None
        if row is None:
            return None
        return row["response"] if isinstance(row, dict) else row[0]

    async def cache_put(self, key: str, task: str, model: str, response: Any) -> None:
        try:
            async with asyncio.timeout(DB_TIMEOUT_S), self.pool.connection(timeout=DB_TIMEOUT_S) as conn:
                await conn.execute(
                    "INSERT INTO llm_cache (key, task, model, response) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET task = EXCLUDED.task, model = EXCLUDED.model, "
                    "response = EXCLUDED.response, created_at = now()",
                    (key, task, model, Jsonb(response)),
                )
        except Exception as exc:
            self._fail("cache_put", exc)

    async def record(self, row: CallRow) -> None:
        try:
            await self._insert_call(row, row.event_id)
        except pg_errors.ForeignKeyViolation:
            # события нет в БД: учёт вызова важнее привязки к событию
            log.warning("шлюз: события %s нет в БД, вызов записан без привязки", row.event_id)
            try:
                await self._insert_call(row, None)
            except Exception as exc:
                self._fail("record", exc)
        except Exception as exc:
            self._fail("record", exc)

    async def _insert_call(self, row: CallRow, event_id: int | None) -> None:
        async with asyncio.timeout(DB_TIMEOUT_S), self.pool.connection(timeout=DB_TIMEOUT_S) as conn:
            await conn.execute(
                "INSERT INTO llm_calls (task, model, prompt_version, tokens_in, tokens_out, cost_rub, "
                "ms, cached, fallback_from, error, lane, trace_id, event_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row.task,
                    row.model,
                    row.prompt_version,
                    row.tokens_in,
                    row.tokens_out,
                    row.cost_rub,
                    row.ms,
                    row.cached,
                    row.fallback_from,
                    row.error[:ERROR_LIMIT] if row.error else None,
                    row.lane,
                    row.trace_id,
                    event_id,
                ),
            )

    async def spent_since(self, start: datetime) -> float | None:
        try:
            async with asyncio.timeout(DB_TIMEOUT_S), self.pool.connection(timeout=DB_TIMEOUT_S) as conn:
                cur = await conn.execute(
                    "SELECT COALESCE(SUM(cost_rub), 0) AS spent FROM llm_calls WHERE created_at >= %s",
                    (start,),
                )
                row = await cur.fetchone()
        except Exception as exc:
            self._fail("spent_since", exc)
            return None
        return float(row["spent"] if isinstance(row, dict) else row[0])


# Медиана задержки и переключения на резерв — без ответов из кэша: это не вызовы модели.
_USAGE_SQL = """
SELECT task, model, count(*)::int AS calls, COALESCE(sum(cost_rub), 0)::float AS cost_rub,
       count(fallback_from) FILTER (WHERE NOT cached)::int AS fallbacks, count(error)::int AS errors,
       COALESCE(round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) FILTER (WHERE NOT cached)), 0)::int
           AS p50_ms
FROM llm_calls
WHERE created_at >= %s
GROUP BY task, model
ORDER BY cost_rub DESC, task, model
"""


async def usage_since(pool: AsyncConnectionPool, start: datetime) -> list[dict[str, Any]]:
    """Расход по задачам и моделям с начала суток бюджета — для админки (ML-3, F9)."""
    async with asyncio.timeout(DB_TIMEOUT_S), pool.connection(timeout=DB_TIMEOUT_S) as conn:
        conn.row_factory = dict_row
        cur = await conn.execute(_USAGE_SQL, (start,))
        return list(await cur.fetchall())


class LazyStore:
    """Хранилище, которое открывает БД в фоне: вызовы модели не ждут её и не зависят от её сбоев.

    Пока пул не открылся, работает память; повторная попытка открыть БД не чаще раза в `retry_s`.
    Открытие не отменяем: отмена посреди `open` оставила бы пул с живыми воркерами.
    """

    def __init__(
        self,
        open_pool: Callable[[], Awaitable[AsyncConnectionPool]],
        *,
        clock: Callable[[], float] = time.monotonic,
        retry_s: float = 60.0,
    ) -> None:
        self._open_pool = open_pool
        self._clock = clock
        self._retry_s = retry_s
        self._memory = MemoryStore()
        self._pg: PgStore | None = None
        self._opening: asyncio.Task[None] | None = None
        self._retry_at = float("-inf")

    async def _open(self) -> None:
        try:
            self._pg = PgStore(await self._open_pool())
        except Exception as exc:
            self._retry_at = self._clock() + self._retry_s
            log.warning("шлюз: БД недоступна (%s), кэш и учёт вызовов пока в памяти", type(exc).__name__)

    def _current(self) -> Store:
        if self._pg is not None:
            return self._pg
        if (self._opening is None or self._opening.done()) and self._clock() >= self._retry_at:
            self._opening = asyncio.create_task(self._open())
        return self._memory

    async def cache_get(self, key: str) -> Any | None:
        return await self._current().cache_get(key)

    async def cache_put(self, key: str, task: str, model: str, response: Any) -> None:
        await self._current().cache_put(key, task, model, response)

    async def record(self, row: CallRow) -> None:
        await self._current().record(row)

    async def spent_since(self, start: datetime) -> float | None:
        # расход других процессов виден только в БД; память отдаёт лишь свои вызовы, это не сверка
        store = self._current()
        return None if store is self._memory else await store.spent_since(start)
