import asyncio
import logging
import signal
import sys

log = logging.getLogger("orientir.worker")


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    log.info("расписание ещё не реализовано (BE-10)")
    await stop.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # на Windows обработчиков сигналов в asyncio нет: Ctrl+C приходит исключением
        pass
