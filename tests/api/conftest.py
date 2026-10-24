import asyncio
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.agent import run
from app.api.deps import MOCK_CSRF_TOKEN
from app.api.main import create_app
from app.settings import Settings


@pytest.fixture(autouse=True)
def no_step_delay(monkeypatch):
    monkeypatch.setattr(run, "STEP_DELAY_S", (0, 0))


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def make(**overrides) -> Settings:
        values = {
            "database_url": "postgresql://unused:unused@localhost:5432/unused",
            "demo_password": SecretStr("test-password"),
            "mock": True,
        }
        return Settings(**{**values, **overrides})

    return make


@pytest.fixture
def mock_app(make_settings) -> FastAPI:
    return create_app(make_settings())


@pytest.fixture
def anon(mock_app) -> Iterator[TestClient]:
    """Клиент без заголовка CSRF."""
    with TestClient(mock_app) as client:
        yield client


@pytest.fixture
def logged_in(anon) -> TestClient:
    """Клиент с cookie сессии, но без заголовка CSRF."""
    response = anon.post("/api/auth/login", json={"login": "anna", "password": "test-password"})
    assert response.status_code == 200
    return anon


@pytest.fixture
def client(logged_in) -> TestClient:
    logged_in.headers["X-CSRF-Token"] = MOCK_CSRF_TOKEN
    return logged_in


def pytest_asyncio_loop_factories(config, item):
    # psycopg в асинхронном режиме не работает на ProactorEventLoop, который по умолчанию в Windows
    return {"selector": asyncio.SelectorEventLoop}
