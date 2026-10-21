import json

import pytest
from pydantic import BaseModel

from app.llm.jsonmode import DOWNGRADE, drop_optional_nulls, parse_json, schema_instruction, strict_schema


class Inner(BaseModel):
    x: int
    y: str | None = None


class Outer(BaseModel):
    a: int
    b: list[str] = []
    inner: Inner | None = None
    items: list[Inner] = []


def test_strict_schema_all_required():
    original = Outer.model_json_schema()
    before = json.dumps(original, sort_keys=True)
    strict = strict_schema(original)

    assert json.dumps(original, sort_keys=True) == before
    assert set(strict["required"]) == {"a", "b", "inner", "items"}
    assert {"type": "null"} in strict["properties"]["b"]["anyOf"]
    inner = strict["$defs"]["Inner"]
    assert set(inner["required"]) == {"x", "y"}
    assert "default" not in json.dumps(strict["properties"]["b"])


def test_drop_optional_nulls_restores_default():
    schema = Outer.model_json_schema()
    data = {"a": 1, "b": None, "inner": None, "items": [{"x": 1, "y": None}]}
    cleaned = drop_optional_nulls(data, schema)
    model = Outer.model_validate(cleaned)
    assert model.b == []
    assert model.inner is None
    assert model.items == [Inner(x=1)]


def test_drop_optional_nulls_keeps_null_of_required_nullable_field():
    class Req(BaseModel):
        v: int | None

    schema = Req.model_json_schema()
    assert drop_optional_nulls({"v": None}, schema) == {"v": None}


@pytest.mark.parametrize(
    "text",
    [
        '{"a": [1, 2]}',
        '```json\n{"a": [1, 2]}\n```',
        '<think>рассуждаю {не json}</think>{"a": [1, 2]}',
        'Вот ответ: {"a": [1, 2]} Готово.',
        '{"a": [1, 2,],}',
    ],
)
def test_parse_json_variants(text):
    assert parse_json(text) == {"a": [1, 2]}


@pytest.mark.parametrize("text", ["", "Не могу ответить", "{oops", '{"a": }'])
def test_parse_json_garbage_raises(text):
    with pytest.raises(json.JSONDecodeError):
        parse_json(text)


def test_downgrade_chain_and_instruction():
    assert DOWNGRADE == {"schema": "object", "object": "prompt", "prompt": None}
    assert '"properties"' in schema_instruction(Outer.model_json_schema())
