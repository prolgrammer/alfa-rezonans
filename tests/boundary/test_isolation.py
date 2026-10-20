import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import app
from app.agent.tools import build_tools
from app.contracts import Access, SessionCtx, UserRole

ALLOWED = {"app.clients.profile.get_profile"}
APP_DIR = Path(app.__file__).parent


def _modules():
    for info in pkgutil.walk_packages(app.__path__, "app."):
        if not info.name.endswith("__main__"):
            yield importlib.import_module(info.name)


def _public_callables(module):
    for name, obj in vars(module).items():
        if name.startswith("_") or getattr(obj, "__module__", None) != module.__name__:
            continue
        if inspect.isfunction(obj):
            yield f"{module.__name__}.{name}", obj
        elif inspect.isclass(obj):
            for method_name, method in vars(obj).items():
                if not method_name.startswith("_") and inspect.isfunction(method):
                    yield f"{module.__name__}.{name}.{method_name}", method


def test_no_function_takes_company_id():
    seen = 0
    offenders = []
    for module in _modules():
        for qualname, func in _public_callables(module):
            seen += 1
            if qualname not in ALLOWED and "company_id" in inspect.signature(func).parameters:
                offenders.append(qualname)
    assert seen > 30
    assert not offenders


def _property_names(schema) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        names |= set(schema.get("properties", {}))
        for value in schema.values():
            names |= _property_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _property_names(value)
    return names


def test_tool_arguments_have_no_company_id():
    ctx = SessionCtx(user_id=1, company_id="C-DEV-01", user_role=UserRole.cfo, access=Access.client)
    tools = build_tools(ctx, 1)
    assert tools
    for name, tool in tools.items():
        assert "company_id" not in _property_names(tool.args.model_json_schema()), name


def test_get_profile_is_called_only_by_agent_tools_and_api():
    allowed = ("app/agent/tools.py", "app/api/", "app/clients/profile.py")
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(APP_DIR.parent).as_posix()
        if re.search(r"\bget_profile\(", path.read_text(encoding="utf-8")) and not rel.startswith(allowed):
            offenders.append(rel)
    assert not offenders
