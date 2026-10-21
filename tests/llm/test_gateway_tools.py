import base64

import httpx
import pytest
from llm_support import FALLBACK, GOOD, PRIMARY, Answer, bodies, route_by_model

import app.llm as llm
from app.llm.errors import AllModelsFailed
from app.llm.types import ToolSpec

VISION_MODEL = "qwen3-vl-30b-a3b-instruct"
TOOLS = [ToolSpec(name="get_rate", description="курс", parameters={"type": "object", "properties": {}})]
MESSAGES = [{"role": "user", "content": "какой курс?"}]


def tool_call_response() -> httpx.Response:
    message = {
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_rate", "arguments": '{"code": "USD"}'},
            }
        ],
    }
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 8},
        },
    )


async def test_tools_step_parses_calls(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: tool_call_response()})
    result = await make_gw().tools_step("chat", MESSAGES, TOOLS)
    assert result.text is None
    assert [(c.id, c.name, c.args) for c in result.tool_calls] == [("call_1", "get_rate", {"code": "USD"})]

    (body,) = bodies(route)
    assert body["tool_choice"] == "auto"
    assert body["tools"] == [
        {
            "type": "function",
            "function": {"name": "get_rate", "description": "курс", "parameters": TOOLS[0].parameters},
        }
    ]
    assert "response_format" not in body
    (row,) = store.rows
    assert (row.task, row.prompt_version, row.lane, row.tokens_in) == ("chat", None, "interactive", 30)


async def test_tools_step_text_answer(api, make_gw):
    route_by_model(
        api, {PRIMARY: httpx.Response(200, json={"choices": [{"message": {"content": "готово"}}]})}
    )
    result = await make_gw().tools_step("chat", MESSAGES, TOOLS)
    assert (result.text, result.tool_calls) == ("готово", [])


async def test_tools_step_skips_models_without_tools(api, make_gw):
    route = route_by_model(api, {PRIMARY: httpx.Response(503, text="down"), FALLBACK: GOOD})
    gw = make_gw(caps={FALLBACK: {"json_object"}})
    with pytest.raises(AllModelsFailed):
        await gw.tools_step("chat", MESSAGES, TOOLS)
    assert bodies(route, FALLBACK) == []


async def test_tools_step_falls_back_to_model_with_tools(api, make_gw, store):
    route_by_model(api, {PRIMARY: httpx.Response(503, text="down"), FALLBACK: tool_call_response()})
    result = await make_gw().tools_step("chat", MESSAGES, TOOLS)
    assert result.tool_calls
    assert store.rows[-1].fallback_from == PRIMARY


async def test_tools_step_empty_answer_is_failure(api, make_gw):
    route_by_model(api, {PRIMARY: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})})
    gw = make_gw(caps={FALLBACK: {"json_object"}})
    with pytest.raises(AllModelsFailed):
        await gw.tools_step("chat", MESSAGES, TOOLS)


async def test_tools_step_not_cached(api, make_gw, store):
    route = route_by_model(api, {PRIMARY: tool_call_response()})
    gw = make_gw()
    await gw.tools_step("chat", MESSAGES, TOOLS)
    await gw.tools_step("chat", MESSAGES, TOOLS)
    assert len(bodies(route)) == 2
    assert [r.cached for r in store.rows] == [False, False]
    assert store._cache == {}


async def test_vision_sends_image_and_caches(api, make_gw, store):
    route = route_by_model(api, {VISION_MODEL: GOOD})
    gw = make_gw()
    png = b"\x89PNG-one"
    assert (await gw.vision("chart_read", "plain", png, Answer)).value == 1
    (body,) = bodies(route)
    text_part, image_part = body["messages"][0]["content"]
    assert text_part["type"] == "text"
    assert image_part == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
    }
    assert "JSON-схеме" in text_part["text"]  # у модели нет json_schema, режим json_object

    await gw.vision("chart_read", "plain", png, Answer)
    assert len(bodies(route)) == 1
    assert store.rows[-1].cached is True

    await gw.vision("chart_read", "plain", b"\x89PNG-two", Answer)
    assert len(bodies(route)) == 2


async def test_vision_without_vision_model_fails(api, make_gw):
    route = route_by_model(api, {VISION_MODEL: GOOD, PRIMARY: GOOD})
    gw = make_gw(caps={VISION_MODEL: {"json_object"}, PRIMARY: {"json_schema"}})
    with pytest.raises(AllModelsFailed):
        await gw.vision("chart_read", "plain", b"x", Answer)
    assert not route.called


def test_strategy(make_gw):
    gw = make_gw()
    assert gw.strategy("insight_write") == "tools"
    assert gw.strategy("chat") == "tools"
    assert gw.strategy("event_card") == "prefetch"
    degraded = make_gw(caps={"glm-5.3-flash": {"json_object"}})
    assert degraded.strategy("insight_write") == "prefetch"
    assert degraded.strategy("chat") == "tools"


def test_public_names():
    for name in llm.__all__:
        assert hasattr(llm, name)
    assert issubclass(llm.LLMNotConfigured, llm.LLMUnavailable)
    assert issubclass(llm.BudgetExceeded, llm.LLMUnavailable)
    assert issubclass(llm.AllModelsFailed, llm.LLMUnavailable)
