from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Lane = Literal["background", "interactive"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolSpec(_Model):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCallRequest(_Model):
    # id нужен агенту, чтобы ответить сообщением role=tool с tool_call_id
    id: str
    name: str
    args: dict[str, Any]


class ToolStepResult(_Model):
    text: str | None
    tool_calls: list[ToolCallRequest]
