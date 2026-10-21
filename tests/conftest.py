import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import conninfo

# Настройки обязательны для get_settings(); тестам хватает заглушек, в сеть и БД они не ходят.
os.environ.setdefault("DATABASE_URL", "postgresql://orientir:orientir@localhost:5432/orientir")
os.environ.setdefault("DEMO_PASSWORD", "test-password")


@pytest.fixture
def test_db_url() -> str:
    # Берём только из окружения, не из deploy/.env: фикстура сносит схему public,
    # и случайно подхваченный чужой адрес стоил бы чужих данных.
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("не задан TEST_DATABASE_URL: тесты с БД пропущены")
    dbname = conninfo.conninfo_to_dict(url).get("dbname", "")
    if not (dbname.startswith("orientir") and dbname.endswith("_test")):
        pytest.fail(f"TEST_DATABASE_URL указывает на базу {dbname!r}: ожидается orientir*_test")
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return url


@pytest.fixture
def db_conn(test_db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(test_db_url, autocommit=True, connect_timeout=10) as conn:
        yield conn
