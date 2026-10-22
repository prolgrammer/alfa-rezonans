import asyncio
import logging
from collections.abc import Awaitable

log = logging.getLogger(__name__)

# Задачи, пережившие свой срок: без ссылки сборщик мусора мог бы убить их на середине
_background: set[asyncio.Future] = set()


def _finished(task: asyncio.Future) -> None:
    _background.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        log.warning("фоновая операция после таймаута завершилась ошибкой", exc_info=error)


async def bounded[T](awaitable: Awaitable[T], timeout: float) -> T:  # noqa: ASYNC109 — не asyncio.timeout: отмены нет
    """Ждёт не дольше timeout, но операцию не отменяет.

    Отмена посреди get_pool() оставила бы открытый пул без владельца, а посреди записи — незавершённую
    транзакцию: поэтому по таймауту операция доживает в фоне, а вызывающий получает TimeoutError.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if task.done():
            # завершилась сама (в том числе тем же TimeoutError): забираем исключение, чтобы не потерять его
            if not task.cancelled():
                task.exception()
        else:
            _background.add(task)
            task.add_done_callback(_finished)
        raise
