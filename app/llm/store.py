import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

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
                        row.event_id,
                    ),
                )
        except Exception as exc:
            self._fail("record", exc)

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
