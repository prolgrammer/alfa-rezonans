from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.types import Slug


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RawItemIn(_Model):
    id: int
    source_id: Slug
    title: str = Field(max_length=500)
    summary: str | None = Field(max_length=2000)
    url: str
    published_at: datetime


class NewsHit(_Model):
    event_id: int
    title: str = Field(max_length=300)
    snippet: str = Field(max_length=500)
    published_at: datetime
    url: str
    score: float


class SourceFragment(_Model):
    n: int = Field(ge=1)
    event_id: int
    raw_item_id: int
    source_name: str = Field(max_length=120)
    url: str
    published_at: datetime
    is_official: bool
    text: str = Field(max_length=1500)
