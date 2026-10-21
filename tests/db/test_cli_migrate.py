import pytest
from typer.testing import CliRunner

from app.cli import app
from app.settings import get_settings

runner = CliRunner()


@pytest.fixture
def cli_env(test_db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_migrate_up_down_up(cli_env):
    first = runner.invoke(app, ["migrate"])
    assert first.exit_code == 0
    assert "0001" in first.output

    assert runner.invoke(app, ["migrate", "up"]).exit_code == 0

    down = runner.invoke(app, ["migrate", "down"])
    assert down.exit_code == 0
    assert "0001" in down.output

    nothing = runner.invoke(app, ["migrate", "down"])
    assert nothing.exit_code == 0

    assert "0001" in runner.invoke(app, ["migrate", "up"]).output


def test_migrate_rejects_unknown_direction_and_steps_for_up(cli_env):
    assert runner.invoke(app, ["migrate", "sideways"]).exit_code == 2
    assert runner.invoke(app, ["migrate", "up", "2"]).exit_code == 2


def test_migrate_reports_connection_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none?connect_timeout=2")
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["migrate"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "ошибка" in result.output
