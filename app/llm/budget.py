import contextlib
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone

from app.llm.errors import BudgetExceeded
from app.llm.store import Store

SYNC_EVERY_S = 60.0
# Москва без перехода на летнее время с 2014 года: фиксированное смещение не зависит от tzdata в образе.
MSK = timezone(timedelta(hours=3), "MSK")


class Budget:
    def __init__(
        self,
        limit_rub: float,
        *,
        store: Store | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(MSK),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit_rub = limit_rub if limit_rub > 0 else None
        self._store = store
        self._now = now
        self._clock = clock
        self._day = now().date()
        self.spent = 0.0
        self.held = 0.0  # резервы вызовов, которые идут прямо сейчас
        self._synced_at = float("-inf")

    def _roll(self) -> None:
        today = self._now().date()
        if today != self._day:
            self._day, self.spent = today, 0.0

    def _day_start(self) -> datetime:
        return datetime(self._day.year, self._day.month, self._day.day, tzinfo=self._now().tzinfo or MSK)

    async def sync(self) -> None:
        """Сверка с llm_calls не чаще раза в минуту: так учитываются вызовы второго процесса."""
        self._roll()
        if self._store is None or self._clock() - self._synced_at < SYNC_EVERY_S:
            return
        self._synced_at = self._clock()
        stored = await self._store.spent_since(self._day_start().astimezone(UTC))
        if stored is not None:
            self.spent = max(self.spent, stored)

    @contextlib.contextmanager
    def hold(self, task: str, essential: bool, reserve_rub: float) -> Iterator[None]:
        """Проверка с резервом и удержание резерва на время вызова: параллельные вызовы видят друг друга."""
        self._roll()
        if not essential and self.limit_rub is not None:
            if self.spent >= self.limit_rub or self.spent + self.held + reserve_rub > self.limit_rub:
                raise BudgetExceeded(
                    f"дневной бюджет LLM исчерпан ({self.spent:.2f} из {self.limit_rub:.0f} ₽, "
                    f"вызов до {reserve_rub:.2f} ₽): {task}"
                )
        self.held += reserve_rub
        try:
            yield
        finally:
            self.held -= reserve_rub

    def add(self, cost_rub: float) -> None:
        self._roll()
        self.spent += cost_rub
