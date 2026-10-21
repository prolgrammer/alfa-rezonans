class LLMUnavailable(Exception):
    """Модель недоступна: потребитель переходит на путь без LLM."""


class LLMNotConfigured(LLMUnavailable):
    """Нет ключа или каталога: запроса к сети не было."""


class BudgetExceeded(LLMUnavailable):
    """Вызов с резервом перейдёт дневной лимит или запрещён ступенью лестницы бюджета."""

    def __init__(self, message: str, *, step: int | None = None) -> None:
        super().__init__(message)
        self.step = step


class RunLimitExceeded(BudgetExceeded):
    """Вызов с резервом перейдёт лимит одного запуска (разбора или ответа чата)."""


class AllModelsFailed(LLMUnavailable):
    """Ни основная, ни резервные модели задачи не дали валидного ответа."""
