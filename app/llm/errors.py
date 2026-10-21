class LLMUnavailable(Exception):
    """Модель недоступна: потребитель переходит на путь без LLM."""


class LLMNotConfigured(LLMUnavailable):
    """Нет ключа или каталога: запроса к сети не было."""


class BudgetExceeded(LLMUnavailable):
    """Вызов с резервом перейдёт дневной лимит."""


class AllModelsFailed(LLMUnavailable):
    """Ни основная, ни резервные модели задачи не дали валидного ответа."""
