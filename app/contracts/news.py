from pydantic import AwareDatetime, Field

from app.contracts._base import ContractModel
from app.contracts.types import Slug


class RawItemIn(ContractModel):
    id: int
    source_id: Slug
    title: str = Field(max_length=500)
    summary: str | None = Field(max_length=2000)
    url: str
    published_at: AwareDatetime


class NewsHit(ContractModel):
    event_id: int
    title: str = Field(max_length=300)
    snippet: str = Field(max_length=500)
    published_at: AwareDatetime
    url: str
    score: float


class SourceFragment(ContractModel):
    n: int = Field(ge=1)
    event_id: int
    raw_item_id: int
    source_name: str = Field(max_length=120)
    url: str
    published_at: AwareDatetime
    is_official: bool
    text: str = Field(max_length=1500)
