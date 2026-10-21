import json

import httpx
import pytest
import respx

from app.llm.config import ProviderCfg
from app.llm.provider import BadRequest, Provider, ProviderError, Transient

BASE = "https://ai.api.cloud.yandex.net/v1"
KEY = "test-key-SECRET-123456"
FOLDER = "folder-SECRET-42"
BODY = {"model": "gpt://x/m/latest", "messages": [{"role": "user", "content": "привет"}]}
OK = {
    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
}


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def pauses():
    return []


@pytest.fixture
def provider(pauses):
    async def sleep(seconds):
        pauses.append(seconds)

    cfg = ProviderCfg(base_url=BASE, headers={"x-data-logging-enabled": "false"})
    p = Provider("yandex", cfg, api_key=KEY, folder=FOLDER, sleep=sleep, clock=lambda: 0.0)
    return p


FAR = 1000.0


async def test_headers_and_body(api, provider):
    route = api.post("/chat/completions").respond(200, json=OK)
    result = await provider.chat(BODY, deadline=FAR)
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Api-Key {KEY}"
    assert request.headers["x-folder-id"] == FOLDER
    assert request.headers["x-data-logging-enabled"] == "false"
    assert json.loads(request.content) == BODY
    assert (result.text, result.tokens_in, result.tokens_out, result.estimated) == ("ok", 7, 3, False)


async def test_retries_on_503_then_ok(api, provider, pauses):
    route = api.post("/chat/completions").mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json=OK)]
    )
    assert (await provider.chat(BODY, deadline=FAR)).text == "ok"
    assert route.call_count == 3
    assert pauses == [1.0, 2.0]


async def test_retries_on_transport_error(api, provider):
    api.post("/chat/completions").mock(
        side_effect=[httpx.ConnectError("нет сети"), httpx.Response(200, json=OK)]
    )
    assert (await provider.chat(BODY, deadline=FAR)).text == "ok"


async def test_429_respects_retry_after_capped(api, provider, pauses):
    api.post("/chat/completions").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200, json=OK)]
    )
    await provider.chat(BODY, deadline=FAR)
    assert pauses == [10.0]


async def test_gives_up_after_three(api, provider):
    route = api.post("/chat/completions").respond(500)
    with pytest.raises(Transient) as info:
        await provider.chat(BODY, deadline=FAR)
    assert route.call_count == 3
    assert info.value.status == 500


async def test_400_is_bad_request_without_retry(api, provider):
    route = api.post("/chat/completions").respond(400, text="Invalid response_format")
    with pytest.raises(BadRequest, match="response_format"):
        await provider.chat(BODY, deadline=FAR)
    assert route.call_count == 1


async def test_401_no_retry(api, provider):
    route = api.post("/chat/completions").respond(401, text="denied")
    with pytest.raises(ProviderError) as info:
        await provider.chat(BODY, deadline=FAR)
    assert not isinstance(info.value, (Transient, BadRequest))
    assert info.value.status == 401
    assert route.call_count == 1


async def test_deadline_stops_retries(api, provider, pauses):
    route = api.post("/chat/completions").respond(503)
    with pytest.raises(Transient):
        await provider.chat(BODY, deadline=3.5)
    assert route.call_count == 1
    assert pauses == []


async def test_deadline_already_passed_sends_nothing(api, provider):
    route = api.post("/chat/completions").respond(200, json=OK)
    with pytest.raises(Transient):
        await provider.chat(BODY, deadline=2.0)
    assert route.call_count == 0


async def test_tool_calls_parsed(api, provider):
    message = {
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_x", "arguments": '{"a": 1}'}},
            {"id": "c2", "type": "function", "function": {"name": "get_y", "arguments": "{oops"}},
        ],
    }
    api.post("/chat/completions").respond(
        200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}], "usage": OK["usage"]}
    )
    result = await provider.chat(BODY, deadline=FAR)
    assert result.text == ""
    assert [(c.id, c.name, c.args) for c in result.tool_calls] == [
        ("c1", "get_x", {"a": 1}),
        ("c2", "get_y", {}),
    ]
    assert result.finish_reason == "tool_calls"


async def test_usage_missing_estimated(api, provider):
    api.post("/chat/completions").respond(200, json={"choices": [{"message": {"content": "x" * 35}}]})
    result = await provider.chat({"messages": [{"role": "user", "content": "y" * 70}]}, deadline=FAR)
    assert result.estimated is True
    assert (result.tokens_in, result.tokens_out) == (20, 10)


def test_mask_hides_key_folder_and_uri(provider):
    text = f"{KEY} {FOLDER} Bearer abcdefgh12345 Api-Key zzzzzzzz9999 gpt://{FOLDER}/x/latest"
    masked = provider.mask(text)
    for secret in (KEY, FOLDER, "abcdefgh12345", "zzzzzzzz9999"):
        assert secret not in masked
    assert "gpt://***/" in masked


async def test_error_text_masked(api, provider):
    api.post("/chat/completions").respond(401, text=f"bad key {KEY} in folder {FOLDER}")
    with pytest.raises(ProviderError) as info:
        await provider.chat(BODY, deadline=FAR)
    assert KEY not in str(info.value)
    assert FOLDER not in str(info.value)


def test_uri_substitutes_folder(provider):
    assert provider.uri("gpt://{folder}/m/latest") == f"gpt://{FOLDER}/m/latest"


def test_lanes_interactive_slot(provider):
    background, interactive = provider.lane("background"), provider.lane("interactive")
    assert background is not interactive
    assert background._value == 3
    assert interactive._value == 1
