import json
import re
from typing import Any, Literal

JsonMode = Literal["schema", "object", "prompt"]
# Ступени понижения режима JSON, когда провайдер отвечает 400 на response_format.
DOWNGRADE: dict[JsonMode, JsonMode | None] = {"schema": "object", "object": "prompt", "prompt": None}

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _nullable(s: dict) -> bool:
    t = s.get("type")
    return (
        t == "null"
        or (isinstance(t, list) and "null" in t)
        or any(isinstance(x, dict) and x.get("type") == "null" for x in s.get("anyOf", []))
    )


def strict_schema(schema: dict) -> dict:
    """Схема для строгого response_format AI Studio: все поля объекта в `required`, необязательные с `null`.

    AI Studio отвечает 400 «all fields must be required» на схему с необязательным полем, а pydantic
    кладёт в `required` только поля без умолчания. Исходная схема не меняется: она же идёт в ключ кэша.
    """

    def fix(node: Any) -> Any:
        if isinstance(node, list):
            return [fix(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {k: fix(v) for k, v in node.items()}
        props = out.get("properties")
        if isinstance(props, dict) and props:
            required = set(node.get("required", []))
            for key, prop in props.items():
                if key in required or not isinstance(prop, dict):
                    continue
                inner = {k: v for k, v in prop.items() if k not in ("default", "title", "description")}
                meta = {k: prop[k] for k in ("title", "description") if k in prop}
                props[key] = (
                    {**inner, **meta} if _nullable(inner) else {"anyOf": [inner, {"type": "null"}], **meta}
                )
            out["required"] = list(props)
        return out

    return fix(schema)


def drop_optional_nulls(data: Any, schema: dict) -> Any:
    """Убирает `null` у полей, которых нет в `required` исходной схемы: pydantic подставит умолчание.

    Пара к `strict_schema`: по строгой схеме модель отвечает `"items": null` там, где у поля умолчание `[]`.
    """
    defs = schema.get("$defs", {})

    def resolve(s: Any) -> dict:
        while isinstance(s, dict) and "$ref" in s:
            s = defs.get(s["$ref"].rsplit("/", 1)[-1], {})
        return s if isinstance(s, dict) else {}

    def walk(value: Any, s: Any) -> Any:
        node = resolve(s)
        alts = [resolve(x) for x in node.get("anyOf", []) if resolve(x).get("type") != "null"]
        if isinstance(value, dict):
            props = node.get("properties") or next(
                (a["properties"] for a in alts if a.get("properties")), None
            )
            if not props:
                return value
            required = set(
                node.get("required") or next((a.get("required", []) for a in alts if a.get("properties")), [])
            )
            return {
                k: walk(v, props.get(k, {}))
                for k, v in value.items()
                if not (v is None and k in props and k not in required)
            }
        if isinstance(value, list):
            items = node.get("items") or next((a["items"] for a in alts if a.get("items")), None)
            return [walk(x, items) for x in value] if items else value
        return value

    return walk(data, schema)


def parse_json(text: str) -> Any:
    """Достаёт JSON из ответа: без <think> и ```-обёртки, без мусора вокруг объекта и висячих запятых."""
    t = _FENCE_RE.sub("", _THINK_RE.sub("", text).strip()).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            raise
        t = t[start : end + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return json.loads(_TRAILING_COMMA_RE.sub(r"\1", t))


def schema_instruction(schema: dict) -> str:
    return (
        "\n\nОтвет — только JSON-объект, без пояснений и без markdown, строго по JSON-схеме:\n"
        + json.dumps(schema, ensure_ascii=False)
    )
