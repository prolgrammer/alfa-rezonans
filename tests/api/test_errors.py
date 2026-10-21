import logging

import pytest
from fastapi.testclient import TestClient

from app.api import mock
from app.errors import NotFound


@pytest.fixture
def lenient(logged_in, mock_app):
    """Клиент, который отдаёт 500 как ответ, а не пробрасывает исключение в тест."""
    with TestClient(mock_app, raise_server_exceptions=False) as client:
        client.cookies.update(logged_in.cookies)
        yield client


def test_not_found_exception_is_404(lenient, monkeypatch):
    async def missing(self, ctx, event_id):
        raise NotFound("нет такого события")

    monkeypatch.setattr(mock.MockBackend, "event_detail", missing)
    response = lenient.get("/api/events/1")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("exc", [KeyError("C-SECRET-77"), IndexError("список пуст"), RuntimeError("сбой")])
def test_other_exceptions_are_500_internal_error_without_leaking(lenient, monkeypatch, caplog, exc):
    async def broken(self, ctx, event_id):
        raise exc

    monkeypatch.setattr(mock.MockBackend, "event_detail", broken)
    with caplog.at_level(logging.ERROR, logger="orientir.api"):
        response = lenient.get("/api/events/1")
    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "internal_error"
    assert str(exc.args[0]) not in response.text
    assert any(type(exc).__name__ in r.getMessage() for r in caplog.records)
    assert all("C-SECRET-77" not in r.getMessage() and not r.exc_info for r in caplog.records)
