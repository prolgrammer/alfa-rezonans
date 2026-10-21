import json
from typing import Any

import httpx
import respx
from pydantic import BaseModel, SecretStr

from app.settings import Settings

BASE = "https://ai.api.cloud.yandex.net/v1"
KEY = "test-key-SECRET-123456"
FOLDER = "folder-SECRET-42"
PRIMARY = "qwen3.6-35b-a3b"  # основная модель задачи event_card
FALLBACK = "glm-5.3-flash"  # её резерв
LAST = "deepseek-v4.1-flash"  # последняя модель цепочки event_card


class Answer(BaseModel):
    value: int
    note: str = ""


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


def completion(content: str | None, tokens: tuple[int, int] = (10, 5)) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]},
        },
    )


GOOD = completion('{"value": 1, "note": "ok"}')


def model_of(request: httpx.Request) -> str:
    # gpt://<каталог>/<имя>/latest; у DeepSeek имя в URI отличается от ключа белого списка
    return json.loads(request.content)["model"].split("/")[3]


def route_by_model(api: respx.MockRouter, handlers: dict[str, Any]) -> respx.Route:
    """Ответ по модели: значение — httpx.Response, список ответов по очереди или функция запроса."""
    counters: dict[str, int] = {}

    def side_effect(request: httpx.Request) -> httpx.Response:
        name = model_of(request)
        handler = handlers[name]
        counters[name] = counters.get(name, 0) + 1
        if isinstance(handler, list):
            return handler[min(counters[name], len(handler)) - 1]
        return handler(request) if callable(handler) else handler

    return api.post("/chat/completions").mock(side_effect=side_effect)


def bodies(route: respx.Route, model: str | None = None) -> list[dict[str, Any]]:
    found = [json.loads(call.request.content) for call in route.calls]
    return [b for b in found if model is None or b["model"].split("/")[3] == model]


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql://x:x@127.0.0.1:1/x",
        "demo_password": "x",
        "llm_api_key": SecretStr(KEY),
        "llm_folder_id": SecretStr(FOLDER),
        "llm_base_url": "",
        "llm_daily_budget_rub": 480.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)
