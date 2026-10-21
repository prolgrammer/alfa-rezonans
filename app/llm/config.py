import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

log = logging.getLogger(__name__)

# Белый список — константа в коде, а не в конфиге: иначе его можно было бы расширить правкой
# того же файла, который им пользуется (ограничение 04 кейса).
ALLOWED_MODELS: frozenset[str] = frozenset(
    {
        "gemma-4-26b-a4b",
        "glm-5.3-flash",
        "qwen3.6-35b-a3b",
        "deepseek-v4.1-flash",
        "qwen3-vl-30b-a3b-instruct",
        "kimi-k2.6",
    }
)
TASKS: tuple[str, ...] = (
    "news_filter",
    "event_card",
    "rank_judge",
    "bank_theses",
    "chart_read",
    "insight_plan",
    "insight_write",
    "insight_critic",
    "chat",
    "profile_text",
    "eval_judge",
    "probe",
)

Cap = Literal["json_schema", "json_object", "tools", "vision"]


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderCfg(_Cfg):
    base_url: str
    concurrency: int = Field(4, ge=1)
    interactive_slots: int = Field(1, ge=0)
    timeout_s: float = Field(60.0, gt=0)
    headers: dict[str, str] = {}


class Price(_Cfg):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    in_: float = Field(alias="in", ge=0)
    out: float = Field(ge=0)


class ModelCfg(_Cfg):
    provider: str
    uri: str
    context: int
    price: Price
    caps: list[Cap]
    reasoning_off: Literal["reasoning_effort", "none"] = "none"


class TaskCfg(_Cfg):
    model: str | None
    fallback: list[str] = []
    temperature: float = Field(0.1, ge=0)
    max_tokens: int = Field(gt=0)
    reasoning: bool = False
    strategy: Literal["tools", "prefetch"] | None = None
    essential: bool = False


class ModelsConfig(_Cfg):
    providers: dict[str, ProviderCfg]
    models: dict[str, ModelCfg]
    tasks: dict[str, TaskCfg]

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        for name, model in self.models.items():
            if name not in ALLOWED_MODELS:
                raise ValueError(f"модель {name!r} не входит в белый список кейса")
            if model.provider not in self.providers:
                raise ValueError(f"у модели {name!r} неизвестный провайдер {model.provider!r}")
        for name, task in self.tasks.items():
            if name not in TASKS:
                raise ValueError(f"неизвестная задача {name!r}")
            chain = [task.model, *task.fallback] if task.model else task.fallback
            for ref in chain:
                if ref not in self.models:
                    raise ValueError(f"задача {name!r} ссылается на модель {ref!r}, которой нет в models")
            if task.model is None and name != "probe":
                raise ValueError(f"у задачи {name!r} не задана модель")
        chart = self.tasks.get("chart_read")
        if chart is not None and chart.model and "vision" not in self.models[chart.model].caps:
            raise ValueError(f"модель {chart.model!r} задачи chart_read должна уметь vision")
        return self

    def task(self, name: str) -> TaskCfg:
        return self.tasks[name]

    def caps(self, model: str, actual: Mapping[str, set[str]]) -> set[str]:
        # фактические возможности (их пишет probe) важнее заявленных в конфиге
        if model in actual:
            return set(actual[model])
        return set(self.models[model].caps)


def load_config(path: Path) -> ModelsConfig:
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_caps(path: Path) -> dict[str, set[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(model): {str(c) for c in caps} for model, caps in raw.items()}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        log.warning("файл возможностей моделей не прочитан, берём заявленные: %s", type(exc).__name__)
        return {}
