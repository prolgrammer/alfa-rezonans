import pytest

from app.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("DEMO_PASSWORD", "demo-secret-value")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_api_key_not_in_repr_and_str(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-very-secret-key")
    settings = get_settings()
    assert "sk-very-secret-key" not in repr(settings)
    assert "sk-very-secret-key" not in str(settings)
    assert settings.llm_api_key.get_secret_value() == "sk-very-secret-key"


def test_demo_password_not_in_repr():
    assert "demo-secret-value" not in repr(get_settings())


def test_mock_flag_from_env(monkeypatch):
    monkeypatch.setenv("MOCK", "1")
    assert get_settings().mock is True


def test_defaults():
    settings = Settings(_env_file=None)
    assert settings.mock is False
    assert settings.llm_daily_budget_rub == 480
    assert settings.warm_enabled is True
    assert settings.fixtures_dir.name == "fixtures"
