from functools import lru_cache

from pydantic import TypeAdapter

from app.settings import get_settings


@lru_cache
def _read(name: str) -> bytes:
    return (get_settings().fixtures_dir / name).read_bytes()


def load_fixture[T](name: str, adapter: TypeAdapter[T]) -> T:
    # Кэшируем сырые байты, а не результат: модель изменяема, и общий объект
    # на всех потребителей позволил бы одному испортить данные другому.
    return adapter.validate_json(_read(name))
