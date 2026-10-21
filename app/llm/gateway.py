from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Lane = Literal["background", "interactive"]


class LLMUnavailable(Exception):
    """Модель недоступна: потребитель переходит на путь без LLM."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolSpec(_Model):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCallRequest(_Model):
    name: str
    args: dict[str, Any]


class ToolStepResult(_Model):
    text: str | None
    tool_calls: list[ToolCallRequest]


class Gateway:
    # заглушка до ML-1
    async def json[M: BaseModel](
        self,
        task: str,
        prompt: str,
        vars: Mapping[str, Any],
        schema: type[M],
        *,
        event_id: int | None = None,
        lane: Lane = "background",
    ) -> M:
        raise LLMUnavailable("шлюз ещё не реализован (ML-1)")

    # заглушка до ML-1
    async def tools_step(
        self,
        task: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        *,
        lane: Lane = "interactive",
    ) -> ToolStepResult:
        raise LLMUnavailable("шлюз ещё не реализован (ML-1)")

    # заглушка до ML-1
    async def vision[M: BaseModel](self, task: str, prompt: str, image_png: bytes, schema: type[M]) -> M:
        raise LLMUnavailable("шлюз ещё не реализован (ML-1)")


gw = Gateway()
