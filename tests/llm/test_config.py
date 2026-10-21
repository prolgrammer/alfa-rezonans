import copy
import json

import pytest
import yaml

from app.llm.config import ALLOWED_MODELS, TASKS, ModelsConfig, load_caps, load_config
from app.settings import REPO_ROOT

MODELS_YAML = REPO_ROOT / "config" / "models.yaml"


def raw() -> dict:
    return copy.deepcopy(yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8")))


def test_repo_config_loads():
    cfg = load_config(MODELS_YAML)
    assert set(cfg.models) == ALLOWED_MODELS
    assert set(cfg.tasks) == set(TASKS)
    assert cfg.task("chart_read").model == "qwen3-vl-30b-a3b-instruct"
    assert all(t.essential is False for t in cfg.tasks.values())
    assert {name for name, t in cfg.tasks.items() if t.reasoning} == {"insight_plan", "insight_critic"}
    assert cfg.task("probe").model is None


def test_unknown_model_rejected():
    data = raw()
    data["models"]["gpt-4o"] = data["models"]["glm-5.3-flash"]
    with pytest.raises(ValueError, match="gpt-4o.*белый список"):
        ModelsConfig.model_validate(data)


def test_unknown_task_rejected():
    data = raw()
    data["tasks"]["mystery"] = data["tasks"]["chat"]
    with pytest.raises(ValueError, match="mystery"):
        ModelsConfig.model_validate(data)


def test_task_refers_missing_model():
    data = raw()
    del data["models"]["kimi-k2.6"]
    with pytest.raises(ValueError, match="kimi-k2.6"):
        ModelsConfig.model_validate(data)


def test_model_refers_missing_provider():
    data = raw()
    data["models"]["glm-5.3-flash"]["provider"] = "nowhere"
    with pytest.raises(ValueError, match="nowhere"):
        ModelsConfig.model_validate(data)


def test_chart_read_needs_vision():
    data = raw()
    data["tasks"]["chart_read"]["model"] = "glm-5.3-flash"
    with pytest.raises(ValueError, match="vision"):
        ModelsConfig.model_validate(data)


def test_task_unknown_name_keyerror():
    with pytest.raises(KeyError):
        load_config(MODELS_YAML).task("nope")


def test_caps_file_overrides_declared(tmp_path):
    cfg = load_config(MODELS_YAML)
    path = tmp_path / "caps.json"
    path.write_text(
        json.dumps(
            {"probed_at": "x", "models": {"glm-5.3-flash": {"status": "ok", "caps": ["json_object"]}}}
        ),
        encoding="utf-8",
    )
    actual = load_caps(path)
    assert cfg.caps("glm-5.3-flash", actual) == {"json_object"}
    assert cfg.caps("kimi-k2.6", actual) == {"json_schema", "json_object", "tools"}


def test_caps_file_missing_or_broken(tmp_path):
    assert load_caps(tmp_path / "absent.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_caps(broken) == {}
    wrong_shape = tmp_path / "list.json"
    wrong_shape.write_text("[1, 2]", encoding="utf-8")
    assert load_caps(wrong_shape) == {}
    # прежний формат {"модель": [caps]} — тоже «не прочитан», а не возможности из ключей
    old_format = tmp_path / "old.json"
    old_format.write_text(json.dumps({"glm-5.3-flash": ["json_object"]}), encoding="utf-8")
    assert load_caps(old_format) == {}
