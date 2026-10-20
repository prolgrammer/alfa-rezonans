from pydantic import ConfigDict

from app.contracts._base import ContractModel
from app.contracts.enums import Access, UserRole


class SessionCtx(ContractModel):
    """Единственный источник компании и роли: из запроса они не берутся никогда."""

    model_config = ConfigDict(frozen=True)

    user_id: int
    company_id: str
    user_role: UserRole
    access: Access
