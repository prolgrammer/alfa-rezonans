import asyncio
from collections.abc import Callable
from typing import Any

import pytest
import respx
from llm_support import BASE, FakeClock, make_settings
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.db import migrate_up
from app.llm.config import load_config
from app.llm.gateway import Gateway
from app.llm.store import MemoryStore
from app.settings import REPO_ROOT, get_settings


def pytest_asyncio_loop_factories(config, item):
    # psycopg в асинхронном режиме не работает на ProactorEventLoop, который по умолчанию в Windows
    return {"selector": asyncio.SelectorEventLoop}


@pytest.fixture
def api():
    # respx по умолчанию падает на немоканном запросе: тест не может уйти в сеть
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def prompts_dir(tmp_path):
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "demo.md").write_text(
        "---\ntask: event_card\nversion: 1\n---\nСкажи {{ what }}", encoding="utf-8"
    )
    (directory / "plain.md").write_text(
        "---\ntask: chart_read\nversion: 1\n---\nПрочитай график", encoding="utf-8"
    )
    return directory


@pytest.fixture
def make_gw(clock, store, prompts_dir) -> Callable[..., Gateway]:
    def factory(**overrides: Any) -> Gateway:
        settings = overrides.pop("settings", None) or make_settings()
        kwargs: dict[str, Any] = {
            "settings": settings,
            "store": store,
            "caps": {},
            "prompts_dir": prompts_dir,
            "clock": clock,
            "sleep": clock.sleep,
        }
        kwargs.update(overrides)
        return Gateway(load_config(REPO_ROOT / "config" / "models.yaml"), **kwargs)

    return factory


@pytest.fixture
async def pg_pool(test_db_url, db_conn):
    migrate_up(db_conn, get_settings().migrations_dir)
    pool = AsyncConnectionPool(test_db_url, kwargs={"row_factory": dict_row}, open=False)
    await pool.open()
    yield pool
    await pool.close()
