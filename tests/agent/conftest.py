import asyncio

import pytest
from agent_helpers import compact, event

from app.agent import guard
from app.agent.context import new_context
from app.contracts import UserRole


def pytest_asyncio_loop_factories(config, item):
    # psycopg в асинхронном режиме не работает на ProactorEventLoop, который по умолчанию в Windows
    return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture
def ctx():
    return new_context(event(1), compact("C-DEV-01"), UserRole.cfo, [])


@pytest.fixture(autouse=True)
def _clean_background():
    # зависшие чтения прошлых тестов не должны попадать в проверки соседних
    guard._background.clear()
    yield
    guard._background.clear()
