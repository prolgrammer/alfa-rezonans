import asyncio
import copy
from typing import Any

import pytest

from app.bank import theses
from app.llm.prompts import Prompts
from app.settings import REPO_ROOT

PROMPTS = Prompts(REPO_ROOT / "config" / "prompts")


def pytest_asyncio_loop_factories(config, item):
    # psycopg в асинхронном режиме не работает на ProactorEventLoop, который по умолчанию в Windows
    return {"selector": asyncio.SelectorEventLoop}


class FakeGateway:
    """Рендерит настоящий промпт и отдаёт ответы по очереди."""

    def __init__(self, *answers: dict[str, Any] | Exception, version: str | None = None) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.variables: list[dict[str, Any]] = []
        self.version = version

    def prompt_version(self, prompt: str) -> str:
        return self.version or PROMPTS.version(prompt)

    async def json(self, task, prompt, variables, schema, **kwargs):
        assert task == prompt == "bank_theses"
        text, _ = PROMPTS.render(prompt, variables)
        self.prompts.append(text)
        self.variables.append(copy.deepcopy(dict(variables)))
        await asyncio.sleep(0)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return schema.model_validate(answer)


@pytest.fixture
def fake(monkeypatch):
    def install(*answers, **kwargs) -> FakeGateway:
        gateway = FakeGateway(*answers, **kwargs)
        monkeypatch.setattr(theses, "gw", gateway)
        return gateway

    return install
