from pydantic import BaseModel, ConfigDict

from app.contracts.enums import Access, UserRole


class SessionCtx(BaseModel):
    """Единственный источник компании и роли: из запроса они не берутся никогда."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: int
    company_id: str
    user_role: UserRole
    access: Access
