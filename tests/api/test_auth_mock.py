import pytest

from app.api.deps import MOCK_CSRF_TOKEN
from app.api.schemas import Feed, Me

FEEDBACK = {"event_id": 1, "verdict": "useful"}


def test_login_sets_httponly_cookie(anon):
    response = anon.post("/api/auth/login", json={"login": "anna", "password": "test-password"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("orientir_session=")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert Me.model_validate(response.json()).login == "anna"


def test_login_with_wrong_password_is_unauthorized(anon):
    response = anon.post("/api/auth/login", json={"login": "anna", "password": "wrong"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "set-cookie" not in response.headers


def test_me_gives_csrf_token_and_expert_session(anon):
    me = Me.model_validate(anon.get("/api/me").json())
    assert me.csrf_token == MOCK_CSRF_TOKEN
    assert (me.company_id, me.user_role, me.access) == ("C-DEV-01", "cfo", "expert")
    assert me.company_name


def test_logout_needs_csrf_and_clears_cookie(anon):
    assert anon.post("/api/auth/logout").status_code == 403
    response = anon.post("/api/auth/logout", headers={"X-CSRF-Token": MOCK_CSRF_TOKEN})
    assert response.status_code == 200
    assert "orientir_session=" in response.headers["set-cookie"]


@pytest.mark.parametrize("headers", [{}, {"X-CSRF-Token": "wrong"}])
def test_mutation_without_valid_csrf_is_forbidden(anon, headers):
    response = anon.post("/api/feedback", json=FEEDBACK, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_mutation_with_csrf_passes(anon):
    response = anon.post("/api/feedback", json=FEEDBACK, headers={"X-CSRF-Token": MOCK_CSRF_TOKEN})
    assert response.status_code == 200


def test_csrf_is_checked_before_body_validation(anon):
    response = anon.post("/api/feedback", json={})
    assert response.status_code == 403


def test_get_does_not_need_csrf(anon):
    assert anon.get("/api/feed").status_code == 200


def test_demo_switch_changes_company_for_me_and_feed(client):
    switched = client.post("/api/demo/switch", json={"company_id": "C-IT-01", "user_role": "treasurer"})
    assert switched.status_code == 200
    me = Me.model_validate(client.get("/api/me").json())
    assert (me.company_id, me.user_role) == ("C-IT-01", "treasurer")
    feed = Feed.model_validate(client.get("/api/feed").json())
    assert [i.event_id for i in feed.items] == [1, 2]


def test_demo_switch_to_unknown_company_is_404(client):
    response = client.post("/api/demo/switch", json={"company_id": "C-XX", "user_role": "cfo"})
    assert response.status_code == 404
    assert client.get("/api/me").json()["company_id"] == "C-DEV-01"


def test_demo_switch_needs_csrf(anon):
    response = anon.post("/api/demo/switch", json={"company_id": "C-IT-01", "user_role": "cfo"})
    assert response.status_code == 403


def test_cors_allows_only_configured_origin(anon):
    allowed = anon.options(
        "/api/feed",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    other = anon.options(
        "/api/feed", headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "GET"}
    )
    assert "access-control-allow-origin" not in other.headers
