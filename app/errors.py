class NotFound(LookupError):
    """Запрошенного объекта нет. Только это исключение API превращает в 404: остальные LookupError
    (KeyError, IndexError) — ошибки кода, они должны давать 500 и запись в журнале."""
