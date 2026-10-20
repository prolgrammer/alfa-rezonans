import pytest
from fastapi.testclient import TestClient

from app.api.errors import ApiError
from app.api.live import LiveBackend
from app.api.main import create_app
from app.contracts import Access, FeedFilters, Period, SessionCtx, UserRole

CTX = SessionCtx(user_id=1, company_id="C-DEV-01", user_role=UserRole.cfo, access=Access.client)


@pytest.fixture
def live(make_settings):
    with TestClient(create_app(make_settings(mock=False))) as client:
        yield client


def test_health_reports_mock_off_without_session(live):
    response = live.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mock": False}


@pytest.mark.parametrize("path", ["/api/feed", "/api/me", "/api/profile", "/api/events/1"])
def test_session_routes_are_unauthorized(live, path):
    response = live.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "BE-9" in response.json()["error"]["message"]


def test_mutation_without_session_is_unauthorized(live):
    response = live.post("/api/feedback", json={"event_id": 1, "verdict": "useful"})
    assert response.status_code == 401


def test_login_is_not_implemented(live):
    response = live.post("/api/auth/login", json={"login": "a", "password": "b"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_implemented"


async def test_live_backend_uses_boundary_functions():
    backend = LiveBackend()
    feed = await backend.feed(CTX, Period.week, FeedFilters())
    assert feed.items and not feed.low_data
    assert (await backend.why(CTX, 1)).chips
    assert (await backend.profile(CTX)).profile.company_id == "C-DEV-01"
    assert (await backend.market("cbr_cny", 30)).points
    with pytest.raises(LookupError):
        await backend.why(CTX, 999)


@pytest.mark.parametrize(
    "call",
    [
        lambda b: b.event_detail(CTX, 1),
        lambda b: b.start_insight(CTX, 1),
        lambda b: b.trace(CTX, 1),
        lambda b: b.bank_page(1, 1),
        lambda b: b.review_queue(),
        lambda b: b.quality(),
    ],
)
async def test_live_backend_without_functions_is_not_implemented(call):
    with pytest.raises(ApiError) as caught:
        await call(LiveBackend())
    assert caught.value.status == 503
    assert caught.value.body.error.code == "not_implemented"
