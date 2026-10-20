import datetime as dt
from typing import Annotated

from pydantic import Field

from app.contracts._base import ContractModel
from app.contracts.enums import FactorDirection, Horizon, ThesisKind
from app.contracts.types import Slug


class ThesisNumber(ContractModel):
    what: str = Field(max_length=120)
    value: float
    unit: str = Field(max_length=20)


class BankThesis(ContractModel):
    text: str = Field(min_length=1, max_length=500)
    quote: str = Field(max_length=600)
    page: int = Field(ge=1)
    kind: ThesisKind
    factors: Annotated[list[Slug], Field(min_length=1, max_length=5)]
    direction: FactorDirection
    horizon: Horizon
    numbers: list[ThesisNumber] = Field(max_length=10)


class ThesisItem(ContractModel):
    id: int
    doc_id: int
    doc_title: str = Field(max_length=300)
    published_on: dt.date
    verified: bool | None
    thesis: BankThesis


class BankChunk(ContractModel):
    id: int
    doc_id: int
    doc_title: str = Field(max_length=300)
    page: int = Field(ge=1)
    text: str = Field(max_length=4000)
    published_on: dt.date | None
