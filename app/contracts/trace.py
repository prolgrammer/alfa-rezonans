from pydantic import Field, JsonValue

from app.contracts._base import ContractModel
from app.contracts.enums import StepKind
from app.contracts.types import Slug


class ToolCallRecord(ContractModel):
    tool: Slug
    args: dict[str, JsonValue]
    result_chars: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    ok: bool
    error: str | None = Field(max_length=300)


class AgentStep(ContractModel):
    seq: int = Field(ge=1)
    kind: StepKind
    title: str = Field(max_length=120)
    input: str = Field(max_length=500)
    output: str = Field(max_length=1000)
    model: str | None
    tokens_in: int = Field(ge=0, default=0)
    tokens_out: int = Field(ge=0, default=0)
    cost_rub: float = Field(ge=0, default=0)
    ms: int = Field(ge=0, default=0)
    tool_call: ToolCallRecord | None = None
