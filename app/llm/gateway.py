from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from app.llm.errors import LLMUnavailable
from app.llm.types import Lane, ToolCallRequest, ToolSpec, ToolStepResult

__all__ = ["Lane", "LLMUnavailable", "ToolCallRequest", "ToolSpec", "ToolStepResult", "Gateway", "gw"]


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
