import pytest
from pydantic import TypeAdapter

from app.api.deps import require_access
from app.api.errors import ApiError, ErrorBody
from app.api.schemas import (
    Compare,
    EventDetail,
    Feed,
    Health,
    InsightStart,
    LlmUsage,
    Me,
    NewsCheckResult,
    Ok,
    Profile,
    ProfileTextPreview,
    Quality,
    ReviewQueueItem,
    SourceHealth,
    ThesisItem,
    ToolCallLog,
    Trace,
    Why,
)
from app.contracts import Access, FeedItem, MarketSeries, SessionCtx, UserRole

# (метод, путь, тело, модель ответа); потоки и PNG проверяют отдельные тесты
ROUTES = [
    ("GET", "/api/health", None, Health),
    ("GET", "/api/me", None, Me),
    ("POST", "/api/auth/login", {"login": "anna", "password": "test-password"}, Me),
    ("POST", "/api/auth/logout", None, Ok),
    ("POST", "/api/demo/switch", {"company_id": "C-IT-01", "user_role": "ceo"}, Me),
    ("GET", "/api/feed", None, Feed),
    ("GET", "/api/feed?period=day&filter=risk&factor=key_rate", None, Feed),
    ("GET", "/api/highlights", None, list[FeedItem]),
    ("GET", "/api/events/1", None, EventDetail),
    ("GET", "/api/events/2", None, EventDetail),
    ("GET", "/api/events/1/why", None, Why),
    ("POST", "/api/events/1/insight", None, InsightStart),
    ("GET", "/api/insights/1/trace", None, Trace),
    ("POST", "/api/news/check", {"text": "ЦБ сохранил ставку"}, NewsCheckResult),
    ("POST", "/api/news/check", {"url": "https://example.test/news/1"}, NewsCheckResult),
    ("GET", "/api/profile", None, Profile),
    ("PATCH", "/api/profile", {"key": "workers", "value": 1900}, Profile),
    ("POST", "/api/profile/text", {"text": "Строим жильё в Москве"}, ProfileTextPreview),
    ("GET", "/api/compare?event=1&a=C-DEV-01&b=C-IT-01", None, Compare),
    ("GET", "/api/market/cbr_key_rate?days=30", None, MarketSeries),
    ("POST", "/api/feedback", {"event_id": 1, "verdict": "miss", "reason": "already_knew"}, Ok),
    ("POST", "/api/expert/reviews", {"insight_id": 1, "action": "edit", "comment": "уточнить дату"}, Ok),
    ("GET", "/api/expert/reviews", None, list[ReviewQueueItem]),
    ("GET", "/api/admin/quality", None, Quality),
    ("GET", "/api/admin/llm/usage", None, LlmUsage),
    ("GET", "/api/admin/tool-calls", None, list[ToolCallLog]),
    ("GET", "/api/admin/sources", None, list[SourceHealth]),
    ("GET", "/api/admin/theses", None, list[ThesisItem]),
    ("PATCH", "/api/admin/theses/8", {"verified": True}, ThesisItem),
]


@pytest.mark.parametrize(
    ("method", "path", "body", "model"), ROUTES, ids=[f"{m} {p}" for m, p, _, _ in ROUTES]
)
def test_route_ok(client, method, path, body, model):
    response = client.request(method, path, json=body)
    assert response.status_code == 200, response.text
    TypeAdapter(model).validate_json(response.content)


def test_bank_page_is_png(client):
    response = client.get("/api/bank/docs/1/page/1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize(
    "path",
    [
        "/api/events/999",
        "/api/events/999/why",
        "/api/market/unknown",
        "/api/insights/999/trace",
        "/api/insights/999/stream",
        "/api/bank/docs/999/page/1",
        "/api/compare?event=1&a=C-DEV-01&b=C-XX",
        "/api/compare?event=999&a=C-DEV-01&b=C-IT-01",
    ],
)
def test_unknown_ids_404(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert ErrorBody.model_validate(response.json()).error.code == "not_found"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/events/999/insight", None),
        ("POST", "/api/feedback", {"event_id": 999, "verdict": "useful"}),
        ("POST", "/api/expert/reviews", {"insight_id": 999, "action": "accept"}),
        ("PATCH", "/api/admin/theses/999", {"verified": True}),
        ("POST", "/api/chat", {"event_id": 999, "question": "Что это значит?"}),
    ],
)
def test_unknown_ids_404_on_mutations(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_validation_error_shape(client):
    response = client.get("/api/feed?period=year")
    assert response.status_code == 422
    error = ErrorBody.model_validate(response.json()).error
    assert error.code == "validation_error"
    assert error.details["errors"][0]["loc"] == ["query", "period"]


@pytest.mark.parametrize(
    "body",
    [{}, {"text": "а", "url": "https://example.test/1"}, {"text": "а", "unknown": 1}],
)
def test_news_check_needs_exactly_one_source(client, body):
    response = client.post("/api/news/check", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unknown_route_has_error_shape(client):
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_require_access_forbids_client():
    client_ctx = SessionCtx(user_id=3, company_id="C-DEV-01", user_role=UserRole.cfo, access=Access.client)
    with pytest.raises(ApiError) as caught:
        await require_access(Access.expert, Access.admin)(client_ctx)
    assert caught.value.status == 403
    assert caught.value.body.error.code == "forbidden"


async def test_require_access_allows_expert():
    expert_ctx = SessionCtx(user_id=3, company_id="C-DEV-01", user_role=UserRole.cfo, access=Access.expert)
    assert await require_access(Access.expert, Access.admin)(expert_ctx) == expert_ctx


def test_profile_patch_is_visible_in_profile(client):
    client.patch("/api/profile", json={"key": "workers", "value": 1900})
    profile = Profile.model_validate(client.get("/api/profile").json())
    workers = next(f for f in profile.profile.features if f.key == "workers")
    assert (workers.value, workers.source) == (1900, "user")


def test_thesis_verification_is_visible_in_list(client):
    client.patch("/api/admin/theses/8", json={"verified": True})
    theses = TypeAdapter(list[ThesisItem]).validate_json(client.get("/api/admin/theses").content)
    assert next(t for t in theses if t.id == 8).verified is True


def test_start_insight_reports_cache_by_feed(client):
    cached = InsightStart.model_validate(client.post("/api/events/1/insight").json())
    assert (cached.cached, cached.status, cached.insight_id) == (True, "done", 1)
    client.post("/api/demo/switch", json={"company_id": "C-IT-01", "user_role": "cfo"})
    fresh = InsightStart.model_validate(client.post("/api/events/1/insight").json())
    assert (fresh.cached, fresh.status) == (False, "running")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/events/2/insight"),
        ("GET", "/api/insights/2/stream"),
        ("GET", "/api/insights/3/trace"),
    ],
)
def test_insight_routes_serve_only_pairs_with_a_fixture(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_each_company_gets_its_own_insight_and_trace(client):
    dev = client.get("/api/insights/1/trace").json()
    dev_stream = client.get("/api/insights/1/stream").text
    client.post("/api/demo/switch", json={"company_id": "C-IT-01", "user_role": "cfo"})
    it = client.get("/api/insights/1/trace").json()
    it_stream = client.get("/api/insights/1/stream").text
    assert len(dev["steps"]) != len(it["steps"])
    assert "Доходность размещения свободных денег" in it_stream
    assert "Доходность размещения свободных денег" not in dev_stream
    assert "C-IT-01" in it["steps"][-1]["output"]


def test_compare_returns_two_different_insights(client):
    compare = Compare.model_validate(client.get("/api/compare?event=1&a=C-DEV-01&b=C-IT-01").json())
    assert compare.a.insight and compare.b.insight
    assert compare.a.insight.insight.impact.direction == "risk"
    assert compare.b.insight.insight.impact.direction == "opportunity"


def test_compare_has_no_insight_for_pair_without_fixture(client):
    compare = Compare.model_validate(client.get("/api/compare?event=2&a=C-DEV-01&b=C-IT-01").json())
    assert compare.a.insight is None and compare.b.insight is None


def test_wrong_method_on_known_path_is_405_in_error_shape(client):
    response = client.get("/api/chat")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "invalid_request"
    assert "POST" in response.json()["error"]["message"]
