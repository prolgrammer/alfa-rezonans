from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    # Переменные окружения приоритетнее файла: так compose и CI переопределяют deploy/.env.
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / "deploy" / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    test_database_url: str = ""

    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_daily_budget_rub: float = 480
    insight_max_rub: float = 4
    chat_max_rub: float = 1.5

    mock: bool = False
    embed_enabled: bool = False
    warm_enabled: bool = True

    session_ttl_hours: int = 12
    demo_password: SecretStr
    cors_origin: str = "http://localhost:5173"

    config_dir: Path = REPO_ROOT / "config"
    fixtures_dir: Path = REPO_ROOT / "tests" / "fixtures"
    migrations_dir: Path = REPO_ROOT / "migrations"
    web_dist: Path = REPO_ROOT / "web" / "dist"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # обязательные поля приходят из окружения
