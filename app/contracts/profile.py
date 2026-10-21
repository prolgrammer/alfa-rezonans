from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue

from app.contracts._base import ContractModel
from app.contracts.enums import UserRole
from app.contracts.types import Slug


class Feature(ContractModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[A-Za-z0-9_]+)*$")
    value: JsonValue
    source: str = Field(pattern=r"^((csv|calc):[A-Za-z0-9_.]+|group|user)$")
    detail: str | None = Field(max_length=300)


class Exposure(ContractModel):
    factor: Slug
    sign: Literal[-1, 0, 1]
    strength: int = Field(ge=1, le=3)
    channels: list[str]
    because: list[str]


class CompanyProfile(ContractModel):
    company_id: str
    version: int = Field(ge=1)
    hash: str = Field(max_length=64)
    completeness: float = Field(ge=0, le=1)
    features: list[Feature]
    exposures: list[Exposure]
    computed_at: AwareDatetime


class CompactProfile(ContractModel):
    sector: Slug | None
    sector_name: str | None
    scale: str | None
    user_role: UserRole
    completeness: float = Field(ge=0, le=1)
    top_exposures: list[Exposure] = Field(max_length=8)
    features: list[Feature] = Field(max_length=20)


class CounterpartyShare(ContractModel):
    """Доля отрасли среди контрагентов; идентификаторов самих контрагентов здесь нет."""

    sector: Slug
    share: float = Field(ge=0, le=1)
    count: int = Field(ge=0)
    regions: list[str] = Field(max_length=5)
