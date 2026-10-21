import json

import pytest
from typer.testing import CliRunner

import app.cli
from app.api.main import app as api_app
from app.settings import REPO_ROOT

runner = CliRunner()
SPEC_PATH = REPO_ROOT / "docs" / "openapi.json"
STREAMS = {("get", "/api/insights/{insight_id}/stream"), ("post", "/api/chat")}
IMAGES = {("get", "/api/bank/docs/{doc_id}/page/{page}")}


def test_openapi_file_is_fresh():
    assert json.loads(SPEC_PATH.read_text(encoding="utf-8")) == json.loads(json.dumps(api_app.openapi()))


def test_openapi_file_has_canonical_format():
    text = SPEC_PATH.read_text(encoding="utf-8")
    expected = json.dumps(json.loads(text), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    assert text == expected


def test_cli_check_passes_on_fresh_file():
    result = runner.invoke(app.cli.app, ["openapi", "--check"])
    assert result.exit_code == 0, result.output


def test_cli_check_fails_on_stale_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app.cli, "REPO_ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    assert runner.invoke(app.cli.app, ["openapi", "--check"]).exit_code == 1

    assert runner.invoke(app.cli.app, ["openapi"]).exit_code == 0
    assert runner.invoke(app.cli.app, ["openapi", "--check"]).exit_code == 0

    spec = tmp_path / "docs" / "openapi.json"
    spec.write_text(spec.read_text(encoding="utf-8").replace("Ориентир API", "Старое имя"), encoding="utf-8")
    assert runner.invoke(app.cli.app, ["openapi", "--check"]).exit_code == 1


def _operations():
    for path, item in api_app.openapi()["paths"].items():
        for method, operation in item.items():
            yield method, path, operation


def test_every_route_documents_its_response():
    seen = 0
    for method, path, operation in _operations():
        seen += 1
        content = operation["responses"]["200"]["content"]
        if (method, path) in STREAMS:
            assert "text/event-stream" in content, path
        elif (method, path) in IMAGES:
            assert "image/png" in content, path
        else:
            schema = json.dumps(content["application/json"]["schema"])
            assert "$ref" in schema, (method, path)
    assert seen == 29


def test_stream_payload_models_are_in_components():
    schemas = api_app.openapi()["components"]["schemas"]
    assert {"AgentStep", "InsightResult", "ChatAnswer", "ErrorBody"} <= set(schemas)
    assert not [name for name in schemas if name.endswith(("-Input", "-Output"))]


@pytest.mark.parametrize("status", ["401", "422"])
def test_error_responses_use_error_body(status):
    text = json.dumps(api_app.openapi())
    assert "HTTPValidationError" not in text
    feed = api_app.openapi()["paths"]["/api/feed"]["get"]["responses"]
    assert feed[status]["content"]["application/json"]["schema"]["$ref"].endswith("/ErrorBody")


def test_generated_response_types_have_defaulted_fields_required():
    schemas = api_app.openapi()["components"]["schemas"]
    assert {"tokens_in", "tokens_out", "cost_rub", "ms", "tool_call"} <= set(schemas["AgentStep"]["required"])
    # в запросах необязательные поля остаются необязательными
    assert schemas["ChatRequest"]["required"] == ["question"]
