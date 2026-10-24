import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.agent.guard import bounded
from app.contracts import AgentStep, InsightContext, StepKind, ToolCallRecord, TraceRecord
from app.db import get_pool
from app.text.verify import clip

log = logging.getLogger(__name__)

TRACE_WRITE_TIMEOUT_S = 2.0

TITLE_LIMIT = 120
INPUT_LIMIT = 500
OUTPUT_LIMIT = 1000


class TraceStore(Protocol):
    async def write(
        self, insight_id: int | None, chat_id: UUID | None, rows: Sequence[tuple[int, str]], *, reset: bool
    ) -> None: ...


class PgTraceStore:
    async def write(
        self, insight_id: int | None, chat_id: UUID | None, rows: Sequence[tuple[int, str]], *, reset: bool
    ) -> None:
        async with (await get_pool()).connection() as conn, conn.transaction():
            # запись, пережившая таймаут, не должна держать соединение пула и блокировки бесконечно
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            await conn.execute("SET LOCAL lock_timeout = '2s'")
            # перезапуск разбора с тем же insight_id: иначе хвост прошлого, более длинного прогона остался бы
            if reset and insight_id is not None:
                await conn.execute("DELETE FROM agent_traces WHERE insight_id = %s", (insight_id,))
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO agent_traces (insight_id, chat_id, seq, step) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    [(insight_id, chat_id, seq, step) for seq, step in rows],
                )


class TraceSink:
    """Копит шаги разбора и пишет их в agent_traces; сбой БД не роняет и не задерживает разбор."""

    def __init__(
        self,
        *,
        insight_id: int | None = None,
        chat_id: UUID | None = None,
        start: int = 0,
        store: TraceStore | None = None,
    ) -> None:
        if insight_id is not None and chat_id is not None:
            raise ValueError("трасса пишется либо по insight_id, либо по chat_id")
        self._insight_id = insight_id
        self._chat_id = chat_id
        self._store: TraceStore | None = None
        if insight_id is not None or chat_id is not None:
            self._store = store or PgTraceStore()
        self._seq = start
        self._steps: list[AgentStep] = []
        self._queue: list[tuple[int, str]] = []
        self._deferred = False
        # запись, пережившая таймаут, ещё идёт в фоне: вторую поверх неё не начинаем
        self._in_flight = False
        # старые строки разбора стираются первой успешной записью; чат дописывается к прежним
        self._reset = insight_id is not None
        self._lock = asyncio.Lock()

    @property
    def steps(self) -> list[AgentStep]:
        return list(self._steps)

    async def record(
        self,
        ctx: InsightContext | None,
        kind: StepKind,
        title: str,
        input: str,
        output: str,
        *,
        model: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_rub: float = 0.0,
        ms: int = 0,
        tool_call: ToolCallRecord | None = None,
    ) -> AgentStep:
        async with self._lock:
            self._seq += 1
            step = AgentStep(
                seq=self._seq,
                kind=kind,
                title=clip(title, TITLE_LIMIT),
                input=clip(input, INPUT_LIMIT),
                output=clip(output, OUTPUT_LIMIT),
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_rub=cost_rub,
                ms=ms,
                tool_call=tool_call,
            )
            self._steps.append(step)
            if self._store is not None:
                # строка сериализуется сразу, поэтому дальнейшие изменения контекста запись не затрагивают
                self._queue.append((step.seq, TraceRecord(step=step, context=ctx).model_dump_json()))
                if not self._deferred:
                    await self._write(final=False)
            return step

    async def flush(self) -> bool:
        async with self._lock:
            if self._store is None or not self._queue:
                return True
            if self._in_flight:
                log.warning("трасса разбора не подтверждена, шагов в очереди: %d", len(self._queue))
                return False
            return await self._write(final=True)

    async def _store_write(self, store: TraceStore, rows: list[tuple[int, str]]) -> None:
        self._in_flight = True
        try:
            await store.write(self._insight_id, self._chat_id, rows, reset=self._reset)
        finally:
            self._in_flight = False

    async def _write(self, *, final: bool) -> bool:
        assert self._store is not None
        rows = list(self._queue)
        try:
            await bounded(self._store_write(self._store, rows), TRACE_WRITE_TIMEOUT_S)
        except Exception:
            self._deferred = True
            if final:
                log.error("трасса разбора не записана, потеряно шагов: %d", len(rows), exc_info=True)
            else:
                log.warning("запись трассы не удалась, шаги копятся до конца разбора", exc_info=True)
            return False
        self._queue = self._queue[len(rows) :]
        self._reset = False
        self._deferred = False
        return True


async def _load_records(insight_id: int) -> list[TraceRecord]:
    async with (await get_pool()).connection() as conn:
        cursor = await conn.execute(
            "SELECT seq, step FROM agent_traces WHERE insight_id = %s ORDER BY seq, id", (insight_id,)
        )
        found = await cursor.fetchall()
    # при повторной записи после обрыва связи у одного seq несколько строк: берём последнюю
    records: dict[int, TraceRecord] = {}
    for row in found:
        raw, seq = row["step"], row["seq"]
        try:
            step = AgentStep.model_validate(raw["step"])
        except (ValidationError, KeyError, TypeError):
            log.warning("строка трассы разбора %s, шаг %s не проходит контракт, пропущена", insight_id, seq)
            continue
        context = None
        # снимок хранит модели других ролей: после их правок он устаревает, а сам шаг остаётся годным
        if raw.get("context") is not None:
            try:
                context = InsightContext.model_validate(raw["context"])
            except ValidationError:
                log.warning(
                    "снимок контекста разбора %s, шаг %s не проходит контракт, отброшен", insight_id, seq
                )
        records[seq] = TraceRecord(step=step, context=context)
    return [records[seq] for seq in sorted(records)]


async def load_trace(insight_id: int) -> list[AgentStep]:
    return [r.step for r in await _load_records(insight_id)]


async def load_context(insight_id: int) -> InsightContext | None:
    contexts = [r.context for r in await _load_records(insight_id) if r.context is not None]
    return contexts[-1] if contexts else None
