import json
from typing import Any

import httpx
import pytest
from llm_support import FOLDER, KEY, bodies, make_settings, model_of
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.llm import gateway as gateway_module
from app.llm import probe
from app.llm.config import load_caps, load_config
from app.llm.errors import BudgetExceeded
from app.llm.probe import Check, ModelReport, ProbeAuthError, ProbeResult
from app.settings import REPO_ROOT

QWEN = "qwen3.6-35b-a3b"
VL = "qwen3-vl-30b-a3b-instruct"
CONFIG = load_config(REPO_ROOT / "config" / "models.yaml")


def reply(content: str | None = None, **message: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content, **message}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        },
    )


def tool_reply(name: str = "get_key_rate", args: str = '{"country": "Россия"}') -> httpx.Response:
    call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": args}}
    return reply(None, tool_calls=[call])


def capable(request: httpx.Request, *, reasons: bool = True) -> httpx.Response:
    """Модель, которая умеет всё и рассуждает, пока её не попросят не рассуждать."""
    body = json.loads(request.content)
    extra = {"reasoning_content": "хм"} if reasons and "reasoning_effort" not in body else {}
    if "tools" in body:
        return tool_reply()
    fmt = (body.get("response_format") or {}).get("type")
    if fmt == "json_schema":
        return reply('{"city": "Париж", "country": "Франция", "population_millions": 2.1}', **extra)
    if fmt == "json_object":
        return reply('```json\n{"city": "Париж", "country": "Франция"}\n```', **extra)
    if isinstance(body["messages"][0]["content"], list):
        return reply("Красный, зелёный", **extra)
    return reply("Париж", **extra)


def route(api, handler):
    return api.post("/chat/completions").mock(side_effect=handler)


async def test_probe_model_all_capabilities(api, make_gw, store):
    calls = route(api, capable)
    report = await probe.probe_model(make_gw(), QWEN)

    assert report.status == "ok"
    assert report.caps == ["json_object", "json_schema", "tools", "vision"]
    assert (report.reasoning_default, report.reasoning_off_param) == (True, True)
    assert report.checks["json_object"].note.startswith("в ответе обёртка")
    assert report.latency_ms is not None
    sent = bodies(calls)
    assert len(sent) == probe.CALLS_PER_MODEL
    # первый вызов — без параметра, дальше — в рабочем режиме шлюза, с отключёнными рассуждениями
    assert "reasoning_effort" not in sent[0]
    assert all(b["reasoning_effort"] == "none" for b in sent[1:])
    assert sent[0]["max_tokens"] == CONFIG.task("probe").max_tokens
    schema_body = next(b for b in sent if (b.get("response_format") or {}).get("type") == "json_schema")
    assert "json" not in schema_body["messages"][0]["content"].lower()
    assert [r.task for r in store.rows] == ["probe"] * probe.CALLS_PER_MODEL
    assert {r.model for r in store.rows} == {QWEN}
    assert report.cost_rub == pytest.approx(sum(r.cost_rub for r in store.rows))


async def test_model_without_reasoning_keeps_param_off(api, make_gw):
    calls = route(api, lambda r: capable(r, reasons=False))
    report = await probe.probe_model(make_gw(), QWEN)
    assert report.reasoning_default is False
    sent = bodies(calls)
    assert [("reasoning_effort" in b) for b in sent].count(True) == 1  # только сама проверка параметра


async def test_unknown_model_is_unavailable(api, make_gw):
    calls = route(api, lambda r: httpx.Response(404, json={"error": "model not found"}))
    report = await probe.probe_model(make_gw(), QWEN)
    assert report.status == "unavailable"
    assert report.caps == sorted(CONFIG.models[QWEN].caps)  # заявленное не трогаем
    assert len(calls.calls) == 1


async def test_bad_request_naming_model_is_unavailable(api, make_gw):
    route(api, lambda r: httpx.Response(400, text="Model gpt://abc/gemma-4/latest not found"))
    assert (await probe.probe_model(make_gw(), QWEN)).status == "unavailable"


async def test_rejected_key_stops_probe(api, make_gw):
    route(api, lambda r: httpx.Response(401, text="denied"))
    with pytest.raises(ProbeAuthError):
        await probe.probe_model(make_gw(), QWEN)


async def test_forbidden_model_is_unavailable(api, make_gw):
    # модель не подключена каталогу — это про неё, а не про ключ
    route(api, lambda r: httpx.Response(403, text="PERMISSION_DENIED"))
    assert (await probe.probe_model(make_gw(), QWEN)).status == "unavailable"


async def test_run_stops_on_rejected_key_but_keeps_finished(api, make_gw):
    def handler(request):
        return capable(request) if model_of(request) == QWEN else httpx.Response(401, text="no")

    route(api, handler)
    api.get("/models").respond(404)
    result = await probe.run_probe(make_gw(), [QWEN, "kimi-k2.6", "glm-5.3-flash"])
    assert [r.name for r in result.reports] == [QWEN]
    assert "отверг ключ" in (result.stopped or "")


async def test_run_stops_on_budget(api, make_gw):
    route(api, capable)
    api.get("/models").respond(404)
    result = await probe.run_probe(make_gw(settings=make_settings(llm_daily_budget_rub=0.0)), [QWEN])
    assert result.reports == [] and "бюджет" in (result.stopped or "")


async def test_one_trace_id_per_run(api, make_gw, store):
    route(api, capable)
    api.get("/models").respond(404)
    await probe.run_probe(make_gw(), [QWEN, "kimi-k2.6"])
    (trace,) = {r.trace_id for r in store.rows}
    assert trace.startswith("probe-")


async def test_truncated_answer_is_unknown_not_missing(api, make_gw):
    # рассуждает и параметр не принимает: проверки оборваны по max_tokens — это не «не умеет»
    def handler(request):
        body = json.loads(request.content)
        if "reasoning_effort" in body:
            return httpx.Response(400, text="unknown parameter reasoning_effort")
        if body["messages"][0]["content"] == probe.SIMPLE_PROMPT:
            return reply("Париж", reasoning_content="долго думаю")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        )

    route(api, handler)
    report = await probe.probe_model(make_gw(), QWEN)
    assert all(report.checks[cap].ok is None for cap in probe.CAPS)
    assert "оборван" in report.checks["json_schema"].note
    assert report.caps == sorted(CONFIG.models[QWEN].caps)


@pytest.mark.parametrize(("tokens_out", "expected"), [(5, False), (25, None), (120, True)])
async def test_reasoning_inferred_from_tokens(api, make_gw, tokens_out, expected):
    def handler(request):
        body = json.loads(request.content)
        if body["messages"][0]["content"] == probe.SIMPLE_PROMPT and "reasoning_effort" not in body:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "Париж"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": tokens_out},
                },
            )
        return capable(request, reasons=False)

    route(api, handler)
    assert (await probe.probe_model(make_gw(), QWEN)).reasoning_default is expected


async def test_param_accepted_but_still_reasoning(api, make_gw):
    route(api, lambda r: reply("Париж", reasoning_content="всё равно думаю"))
    report = await probe.probe_model(make_gw(), QWEN)
    assert report.reasoning_off_param is False
    assert "всё равно" not in report.checks["reasoning_off"].note
    assert "параметр принят, но модель рассуждала" in report.checks["reasoning_off"].note


@pytest.mark.parametrize(("status", "expected"), [(422, False), (405, False), (429, None), (500, None)])
async def test_capability_check_status(api, make_gw, status, expected):
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(status, text="nope") if "tools" in body else capable(request)

    route(api, handler)
    assert (await probe.probe_model(make_gw(), QWEN)).checks["tools"].ok is expected


async def test_unexpected_client_error_is_recorded(api, make_gw, store):
    def handler(request):
        raise RuntimeError(f"сломалось {KEY}")

    route(api, handler)
    report = await probe.probe_model(make_gw(), QWEN)
    assert report.status == "error"
    assert store.rows and KEY not in (store.rows[0].error or "")


async def test_simple_failing_is_error(api, make_gw):
    route(api, lambda r: httpx.Response(503, text="busy"))
    report = await probe.probe_model(make_gw(), QWEN)
    assert report.status == "error"
    assert report.caps == sorted(CONFIG.models[QWEN].caps)


async def test_mixed_results(api, make_gw):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if (body.get("response_format") or {}).get("type") == "json_schema":
            return httpx.Response(400, text="response_format json_schema is not supported")
        if "tools" in body:
            return httpx.Response(503, text="busy")
        if "reasoning_effort" in body:
            return httpx.Response(400, text="unknown parameter reasoning_effort")
        if isinstance(body["messages"][0]["content"], list):
            return reply("Не вижу картинку")
        return capable(request)

    calls = route(api, handler)
    report = await probe.probe_model(make_gw(), QWEN)
    assert report.checks["json_schema"].ok is False
    assert report.checks["tools"].ok is None
    assert report.checks["vision"].ok is False
    assert report.reasoning_off_param is False
    # json_schema и vision отняты, tools не проверен — остаётся заявленным
    assert report.caps == ["json_object", "tools"]
    assert all("reasoning_effort" not in b for b in bodies(calls)[2:])


async def test_text_instead_of_tool_call(api, make_gw):
    def handler(request):
        body = json.loads(request.content)
        return reply("Ставка 16%") if "tools" in body else capable(request)

    route(api, handler)
    report = await probe.probe_model(make_gw(), QWEN)
    assert report.checks["tools"].ok is False
    assert "текстом" in report.checks["tools"].note


async def test_wrong_tool_arguments(api, make_gw):
    def handler(request):
        body = json.loads(request.content)
        return tool_reply(args="{}") if "tools" in body else capable(request)

    route(api, handler)
    assert (await probe.probe_model(make_gw(), QWEN)).checks["tools"].ok is False


async def test_reasoning_eats_max_tokens(api, make_gw):
    def handler(request):
        body = json.loads(request.content)
        if "reasoning_effort" not in body:
            return httpx.Response(
                200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
            )
        return capable(request)

    route(api, handler)
    report = await probe.probe_model(make_gw(), QWEN)
    assert report.checks["simple"].ok is False
    assert "рассуждения" in report.checks["simple"].note
    assert (report.reasoning_default, report.reasoning_off_param) == (True, True)


async def test_vision_image_is_png_with_two_halves(api, make_gw):
    calls = route(api, capable)
    await probe.probe_model(make_gw(), QWEN)
    image = next(b for b in bodies(calls) if isinstance(b["messages"][0]["content"], list))
    url = image["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,iVBORw0KGgo")


async def test_budget_exhausted_raises(api, make_gw):
    route(api, capable)
    gw = make_gw(settings=make_settings(llm_daily_budget_rub=0.0))
    with pytest.raises(BudgetExceeded):
        await probe.probe_model(gw, QWEN)


async def test_error_text_masks_key(api, make_gw, store):
    route(api, lambda r: httpx.Response(503, text=f"bad key {KEY}"))
    report = await probe.probe_model(make_gw(), QWEN)
    assert KEY not in report.note
    assert all(KEY not in (r.error or "") for r in store.rows)


async def test_run_probe_reads_catalog(api, make_gw):
    route(api, capable)
    api.get("/models").respond(200, json={"data": [{"id": "gpt://folder-SECRET-42/qwen3.6-35b-a3b/latest"}]})
    seen: list[str] = []
    result = await probe.run_probe(make_gw(), [QWEN], on_model=lambda r: seen.append(r.name))
    assert seen == [QWEN]
    assert result.catalog == ["gpt://***/qwen3.6-35b-a3b/latest"]


async def test_run_probe_without_catalog(api, make_gw):
    route(api, capable)
    api.get("/models").respond(404)
    assert (await probe.run_probe(make_gw(), [QWEN])).catalog is None


def test_final_caps():
    checks = {"json_schema": Check(ok=False), "tools": Check(ok=None), "vision": Check(ok=True)}
    assert probe.final_caps(["json_schema", "json_object", "tools"], checks) == [
        "json_object",
        "tools",
        "vision",
    ]


def report(name: str, **kwargs: Any) -> ModelReport:
    return ModelReport(name=name, uri=CONFIG.models[name].uri, **kwargs)


def test_write_caps_merges_and_gateway_reads_it(tmp_path):
    path = tmp_path / "caps.json"
    first = ProbeResult(None, [report(QWEN, caps=["json_object"]), report(VL, caps=["vision"])])
    probe.write_caps(path, first)
    second = ProbeResult(["gpt://***/kimi-k2.6"], [report(VL, caps=["json_object", "vision"])])
    probe.write_caps(path, second)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["models"]) == {QWEN, VL}
    assert data["catalog"] == ["gpt://***/kimi-k2.6"]
    assert "{folder}" in data["models"][QWEN]["uri"]
    actual = load_caps(path)
    assert CONFIG.caps(QWEN, actual) == {"json_object"}
    assert CONFIG.caps(VL, actual) == {"json_object", "vision"}


def test_write_caps_replaces_broken_file(tmp_path):
    path = tmp_path / "caps.json"
    path.write_text("{oops", encoding="utf-8")
    probe.write_caps(path, ProbeResult(None, [report(QWEN, caps=["tools"])]))
    assert load_caps(path) == {QWEN: {"tools"}}


def test_discrepancies():
    reports = [
        report("gemma-4-26b-a4b", status="unavailable"),
        report(
            QWEN, caps=["json_object", "tools"], reasoning_default=False, checks={"tools": Check(ok=None)}
        ),
        report(
            "glm-5.3-flash",
            caps=sorted(CONFIG.models["glm-5.3-flash"].caps),
            reasoning_default=True,
            reasoning_off_param=True,
        ),
        report(VL, caps=["json_object"]),
    ]
    catalog = ["gpt://***/gemma-3-27b-it/latest", "gpt://***/qwen3.6-35b-a3b/latest"]
    lines = probe.discrepancies(CONFIG, ProbeResult(catalog, reports))
    text = "\n".join(lines)
    assert "gemma-4-26b-a4b: URI" in text and "gemma-3-27b-it" in text
    assert f"{QWEN}: убрать из caps json_schema, vision" in text
    assert f"{QWEN}: не проверено (сбой) — tools" in text
    assert f"{QWEN}: без рассуждений по умолчанию" in text
    assert "glm-5.3-flash: рассуждает по умолчанию" in text
    assert f"chart_read: у {VL} нет vision" in text


def test_catalog_matches():
    catalog = ["gpt://***/qwen3.6-35b-a3b/latest", "gpt://***/qwen3-30b-a3b", "deepseek-v4.1-flash"]
    matches = probe.catalog_matches(CONFIG, catalog)
    assert matches[QWEN] == (True, ["qwen3-30b-a3b", "qwen3.6-35b-a3b"])
    assert matches["deepseek-v4.1-flash"][0] is True
    assert matches["kimi-k2.6"] == (False, [])


def test_estimate_covers_every_call():
    rows = probe.estimate(CONFIG, [QWEN, "kimi-k2.6"])
    assert [(name, calls) for name, calls, _ in rows] == [(QWEN, 8), ("kimi-k2.6", 8)]
    price = CONFIG.models[QWEN].price
    out_only = 8 * CONFIG.task("probe").max_tokens * price.out / 1000
    assert rows[0][2] > out_only


def test_render_table_marks():
    lines = probe.render_table(
        [report(QWEN, checks={"json_schema": Check(ok=True), "tools": Check(ok=None)}, latency_ms=812)]
    )
    assert "да" in lines[1] and "?" in lines[1] and "812" in lines[1]


runner = CliRunner()


def test_cli_dry_run_needs_no_key(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run не должен звать сеть")

    monkeypatch.setattr(probe, "run_probe", forbidden)
    result = runner.invoke(cli_app, ["llm", "probe", "--dry-run", "--model", QWEN])
    assert result.exit_code == 0, result.output
    assert QWEN in result.output and "Итого: 8 вызовов" in result.output


def test_cli_unknown_model():
    result = runner.invoke(cli_app, ["llm", "probe", "--model", "gpt-4o"])
    assert result.exit_code == 2


def test_cli_without_key(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_settings", lambda: make_settings(llm_api_key=""))
    result = runner.invoke(cli_app, ["llm", "probe", "--model", QWEN])
    assert result.exit_code == 1
    assert "LLM_API_KEY" in result.output


def test_cli_full_run(api, monkeypatch, tmp_path):
    async def no_db():
        raise OSError("нет БД")

    target = tmp_path / ".models_caps.json"
    monkeypatch.setattr(gateway_module, "get_settings", lambda: make_settings())
    monkeypatch.setattr(gateway_module, "CAPS_FILE", target)
    monkeypatch.setattr("app.cli.get_pool", no_db)
    route(api, capable)
    api.get("/models").respond(404)

    result = runner.invoke(cli_app, ["llm", "probe", "--model", QWEN])
    assert result.exit_code == 0, result.output
    assert "Каталог провайдера: недоступен" in result.output
    assert "не попадут в llm_calls" in result.output
    assert load_caps(target) == {QWEN: {"json_object", "json_schema", "tools", "vision"}}
    written = target.read_text(encoding="utf-8")
    for secret in (KEY, FOLDER):
        assert secret not in result.output and secret not in written


def test_cli_rejected_key_writes_nothing(api, monkeypatch, tmp_path):
    async def no_db():
        raise OSError("нет БД")

    target = tmp_path / ".models_caps.json"
    monkeypatch.setattr(gateway_module, "get_settings", lambda: make_settings())
    monkeypatch.setattr(gateway_module, "CAPS_FILE", target)
    monkeypatch.setattr("app.cli.get_pool", no_db)
    route(api, lambda r: httpx.Response(401, text="no"))
    api.get("/models").respond(401)

    result = runner.invoke(cli_app, ["llm", "probe", "--model", QWEN])
    assert result.exit_code == 1
    assert not target.exists()


def test_cli_writes_finished_models_when_stopped(api, monkeypatch, tmp_path):
    async def no_db():
        raise OSError("нет БД")

    def handler(request):
        return capable(request) if model_of(request) == QWEN else httpx.Response(401, text="no")

    target = tmp_path / ".models_caps.json"
    monkeypatch.setattr(gateway_module, "get_settings", lambda: make_settings())
    monkeypatch.setattr(gateway_module, "CAPS_FILE", target)
    monkeypatch.setattr("app.cli.get_pool", no_db)
    route(api, handler)
    api.get("/models").respond(404)

    result = runner.invoke(cli_app, ["llm", "probe", "--model", QWEN, "--model", "kimi-k2.6"])
    assert result.exit_code == 1
    assert "прогон остановлен" in result.output
    assert set(load_caps(target)) == {QWEN}
