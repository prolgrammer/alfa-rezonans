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
def client(anon) -> TestClient:
    anon.headers["X-CSRF-Token"] = MOCK_CSRF_TOKEN
    return anon
