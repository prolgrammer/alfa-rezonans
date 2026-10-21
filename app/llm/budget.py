import contextlib
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone

from app.llm.errors import BudgetExceeded
from app.llm.store import Store
from app.llm.types import Lane

SYNC_EVERY_S = 60.0
# Москва без перехода на летнее время с 2014 года: фиксированное смещение не зависит от tzdata в образе.
MSK = timezone(timedelta(hours=3), "MSK")

# Лестница деградации (ML-3): доля дневного лимита → ступень.
LADDER: tuple[tuple[float, int], ...] = ((1.0, 4), (0.95, 3), (0.85, 2), (0.70, 1))
WARM_TASKS = frozenset({"insight_plan", "insight_write", "insight_critic"})
# массовые разовые прогоны: им не место в последней трети бюджета, остаток — интерактиву
BULK_TASKS = frozenset({"bank_theses", "chart_read", "eval_judge"})


def ladder_step(spent_rub: float, limit_rub: float) -> int:
    if limit_rub <= 0:
        return 4
    share = spent_rub / limit_rub
    return next((step for threshold, step in LADDER if share >= threshold), 0)


def ladder_block(step: int, task: str, lane: Lane, *, priority: bool = False) -> str | None:
    """Почему вызов запрещён на этой ступени, или None, если разрешён."""
    background = lane == "background"
    if step >= 4:
        return "лимит исчерпан, разбор без LLM"
    if step >= 3 and background:
        return "95%: только интерактивные вызовы"
    if step >= 3 and task == "insight_critic":
        return "95%: разбор без критика"
    if step >= 2 and task == "rank_judge":
        return "85%: судья ленты выключен"
    if step >= 2 and task == "event_card" and background and not priority:
        return "85%: карточки только для официальных источников"
    if step >= 1 and background and task in WARM_TASKS | BULK_TASKS:
        return "70%: прогрев и массовые прогоны остановлены"
    return None


@dataclass(frozen=True)
class BudgetState:
    day: date
    limit_rub: float
    spent_rub: float
    step: int
    since: datetime  # начало суток бюджета, UTC


class Budget:
    def __init__(
        self,
        limit_rub: float,
        *,
        store: Store | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(MSK),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # 0 и меньше: тратить нельзя (оператор, выставивший 0, не имеет в виду «без лимита»)
        self.limit_rub = limit_rub
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
        stored = await self._store.spent_since(self._day_start().astimezone(UTC))
        if stored is None:
            # сверки не было: следующий вызов попробует снова, не ждём минуту
            return
        self._synced_at = self._clock()
        self.spent = max(self.spent, stored)

    @contextlib.contextmanager
    def hold(self, task: str, essential: bool, reserve_rub: float) -> Iterator[None]:
        """Проверка с резервом и удержание резерва на время вызова: параллельные вызовы видят друг друга."""
        self._roll()
        if not essential and (
            self.spent >= self.limit_rub or self.spent + self.held + reserve_rub > self.limit_rub
        ):
            raise BudgetExceeded(
                f"дневной бюджет LLM исчерпан ({self.spent:.2f} из {self.limit_rub:.0f} ₽, "
                f"вызов до {reserve_rub:.2f} ₽): {task}",
                step=self.step,
            )
        self.held += reserve_rub
        try:
            yield
        finally:
            self.held -= reserve_rub

    @property
    def step(self) -> int:
        # по факту, без резервов: резерв — оценка сверху, ступень не должна мигать от параллельных вызовов
        self._roll()
        return ladder_step(self.spent, self.limit_rub)

    def check(self, task: str, lane: Lane, essential: bool, *, priority: bool = False) -> None:
        if essential:
            return
        step = self.step
        reason = ladder_block(step, task, lane, priority=priority)
        if reason is not None:
            raise BudgetExceeded(
                f"бюджет LLM, ступень {step} ({self.spent:.2f} из {self.limit_rub:.0f} ₽): {reason} — {task}",
                step=step,
            )

    def state(self) -> BudgetState:
        self._roll()
        return BudgetState(
            self._day, self.limit_rub, round(self.spent, 4), self.step, self._day_start().astimezone(UTC)
        )

    def add(self, cost_rub: float) -> None:
        self._roll()
        self.spent += cost_rub
