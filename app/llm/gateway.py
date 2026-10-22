import asyncio
import base64
import contextlib
import contextvars
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app.db import get_pool
from app.llm.budget import Budget, BudgetState
from app.llm.config import ALLOWED_MODELS, ModelCfg, ModelsConfig, TaskCfg, load_caps, load_config
from app.llm.errors import (
    AllModelsFailed,
    BudgetExceeded,
    LLMNotConfigured,
    LLMUnavailable,
    RunLimitExceeded,
)
from app.llm.jsonmode import (
    DOWNGRADE,
    JsonMode,
    drop_optional_nulls,
    parse_json,
    schema_instruction,
    strict_schema,
)
from app.llm.prompts import Prompts
from app.llm.provider import BadRequest, ChatResult, Provider, ProviderError, SlotTimeout, estimate_tokens
from app.llm.store import CallRow, LazyStore, Store
from app.llm.types import Lane, ToolCallRequest, ToolSpec, ToolStepResult
from app.settings import REPO_ROOT, Settings, get_settings

__all__ = [
    "AllModelsFailed",
    "Breaker",
    "BudgetExceeded",
    "Gateway",
    "LLMNotConfigured",
    "LLMUnavailable",
    "Lane",
    "RunLimitExceeded",
    "Scope",
    "ScopeCall",
    "ToolCallRequest",
    "ToolSpec",
    "ToolStepResult",
    "gw",
    "llm_scope",
]

log = logging.getLogger(__name__)

# Общий дедлайн вызова на всю цепочку моделей: разбор по клику укладывается в 45 с (ML-13).
LANE_DEADLINE_S: dict[Lane, float] = {"background": 180.0, "interactive": 40.0}
MIN_WINDOW_S = 3.0
VALIDATION_ATTEMPTS = 2
ERROR_LIMIT = 500
# Сколько токенов занимает картинка в оценке резерва: точное число зависит от модели и размера.
IMAGE_TOKENS = 2000
RAW_DEADLINE_S = 60.0
CAPS_FILE = REPO_ROOT / ".models_caps.json"


class Breaker:
    def __init__(
        self, threshold: int = 5, cooldown_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def state(self, model: str) -> Literal["closed", "open", "half_open"]:
        if self._failures.get(model, 0) < self.threshold:
            return "closed"
        return "open" if self._clock() < self._open_until[model] else "half_open"

    def allows(self, model: str) -> bool:
        return self.state(model) != "open"

    def failure(self, model: str) -> None:
        self._failures[model] = self._failures.get(model, 0) + 1
        if self._failures[model] >= self.threshold:
            # неудачный пробный запрос в half_open снова размыкает предохранитель на полную паузу
            self._open_until[model] = self._clock() + self.cooldown_s

    def success(self, model: str) -> None:
        self._failures.pop(model, None)
        self._open_until.pop(model, None)


@dataclass
class ScopeCall:
    task: str
    model: str
    ms: int
    cost_rub: float
    cached: bool
    fallback_from: str | None


@dataclass
class Scope:
    trace_id: str
    event_id: int | None = None
    # лимит одного запуска: разбор — INSIGHT_MAX_RUB, ответ чата — CHAT_MAX_RUB (ML-3)
    limit_rub: float | None = None
    cost_rub: float = 0.0
    held: float = 0.0  # резервы вызовов области, которые идут прямо сейчас
    tokens_in: int = 0
    tokens_out: int = 0
    calls: list[ScopeCall] = field(default_factory=list)


_scope: contextvars.ContextVar[Scope | None] = contextvars.ContextVar("llm_scope", default=None)


@contextlib.contextmanager
def _run_hold(task: str, reserve_rub: float) -> Iterator[None]:
    """Лимит запуска: новый вызов не начинается, если потрачено и зарезервировано уже не меньше лимита.

    Резерв самого вызова не входит: он оценка сверху (max_tokens, ×2 на рассуждения), и с ним
    первый же шаг разбора на GLM (~3,6 ₽ резерва) не прошёл бы лимит 4 ₽, хотя стоит 1–2 ₽.
    Перерасход — по вызову на каждый, начатый, пока лимит не был занят; строгий лимит держит дневной бюджет.
    """
    scope = _scope.get()
    if scope is None or scope.limit_rub is None:
        yield
        return
    if scope.cost_rub + scope.held >= scope.limit_rub:
        raise RunLimitExceeded(
            f"лимит запуска исчерпан ({scope.cost_rub:.2f} ₽ потрачено, {scope.held:.2f} ₽ в работе, "
            f"лимит {scope.limit_rub:.2f} ₽): {task}"
        )
    scope.held += reserve_rub
    try:
        yield
    finally:
        scope.held -= reserve_rub


@contextlib.contextmanager
def llm_scope(
    *, trace_id: str | None = None, event_id: int | None = None, limit_rub: float | None = None
) -> Iterator[Scope]:
    scope = Scope(trace_id=trace_id or uuid.uuid4().hex[:16], event_id=event_id, limit_rub=limit_rub)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def current_scope() -> Scope | None:
    return _scope.get()


class _Failed(Exception):
    """Модель не дала валидного ответа; текст уже замаскирован."""

    def __init__(self, message: str, *, model_fault: bool = True) -> None:
        super().__init__(message)
        # сбой не по вине модели (очередь, дедлайн, чужой запрос) предохранитель не считает
        self.model_fault = model_fault


@dataclass
class RawAnswer:
    """Ответ одной модели без кэша и резервных моделей: результат или ошибка (текст замаскирован)."""

    result: ChatResult | None
    error: ProviderError | None
    ms: int
    cost_rub: float


@dataclass
class _Usage:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class _Request:
    task: str
    cfg: TaskCfg
    lane: Lane
    messages: list[dict[str, Any]]
    schema: type[BaseModel] | None  # None: шаг вызова функций, ответ не JSON-объект
    prompt_version: str | None
    event_id: int | None
    trace_id: str
    tools: list[dict[str, Any]] | None = None
    need_cap: str | None = None  # возможность, без которой модель цепочки пропускается
    priority: bool = False  # официальный источник: карточка нужна и на ступени 85%


def _text_of(message: dict[str, Any]) -> tuple[str, int]:
    content = message.get("content")
    if isinstance(content, str):
        return content, 0
    if isinstance(content, list):
        parts = [p for p in content if isinstance(p, dict)]
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        return text, sum(1 for p in parts if p.get("type") == "image_url")
    return "", 0


def _hash_images(message: dict[str, Any]) -> dict[str, Any]:
    # в ключ кэша идёт sha256 картинки, а не она сама
    content = message.get("content")
    if not isinstance(content, list):
        return message
    parts = [
        {"type": "image_url", "sha256": hashlib.sha256(p["image_url"]["url"].encode()).hexdigest()}
        if p.get("type") == "image_url"
        else p
        for p in content
    ]
    return {**message, "content": parts}


def _with_instruction(messages: list[dict[str, Any]], instruction: str) -> list[dict[str, Any]]:
    first, rest = messages[0], messages[1:]
    content = first["content"]
    if isinstance(content, str):
        content = content + instruction
    else:
        content = [
            {**p, "text": p["text"] + instruction} if p.get("type") == "text" and i == 0 else p
            for i, p in enumerate(content)
        ]
    return [{**first, "content": content}, *rest]


class Gateway:
    def __init__(
        self,
        config: ModelsConfig,
        *,
        settings: Settings | None = None,
        store: Store | None = None,
        caps: Mapping[str, set[str]] | None = None,
        prompts_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self._settings = settings
        self._store = store
        self._caps = caps
        self._prompts = Prompts(prompts_dir or REPO_ROOT / "config" / "prompts")
        self._clock = clock
        self._now = now
        self._sleep = sleep
        self.breaker = Breaker(clock=clock)
        self._json_mode: dict[str, JsonMode] = {}
        self._no_reasoning_param: set[str] = set()
        self._providers: dict[str, Provider] = {}
        self._budget: Budget | None = None
        self._init_lock = asyncio.Lock()

    async def json[M: BaseModel](
        self,
        task: str,
        prompt: str,
        vars: Mapping[str, Any],
        schema: type[M],
        *,
        event_id: int | None = None,
        lane: Lane = "background",
        priority: bool = False,
    ) -> M:
        cfg = self.config.task(task)
        text, version = self._prompts.render(prompt, vars)
        req = self._request(task, cfg, lane, [{"role": "user", "content": text}], schema, version, event_id)
        req.priority = priority
        return await self._run(req)

    async def tools_step(
        self,
        task: str,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        *,
        lane: Lane = "interactive",
    ) -> ToolStepResult:
        cfg = self.config.task(task)
        req = self._request(task, cfg, lane, list(messages), None, None, None)
        req.tools = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in tools
        ]
        req.need_cap = "tools"
        return await self._run(req)

    async def vision[M: BaseModel](self, task: str, prompt: str, image_png: bytes, schema: type[M]) -> M:
        cfg = self.config.task(task)
        text, version = self._prompts.render(prompt, {})
        url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
        content = [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": url}}]
        req = self._request(
            task, cfg, "background", [{"role": "user", "content": content}], schema, version, None
        )
        req.need_cap = "vision"
        return await self._run(req)

    def prompt_version(self, prompt: str) -> str:
        return self._prompts.version(prompt)

    def strategy(self, task: str) -> Literal["tools", "prefetch"]:
        cfg = self.config.task(task)
        if (
            cfg.strategy == "tools"
            and cfg.model
            and "tools" in self.config.caps(cfg.model, self._actual_caps())
        ):
            return "tools"
        return "prefetch"

    def _request(
        self,
        task: str,
        cfg: TaskCfg,
        lane: Lane,
        messages: list[dict[str, Any]],
        schema: type[BaseModel] | None,
        version: str | None,
        event_id: int | None,
    ) -> _Request:
        scope = _scope.get()
        return _Request(
            task=task,
            cfg=cfg,
            lane=lane,
            messages=messages,
            schema=schema,
            prompt_version=version,
            event_id=event_id if event_id is not None else (scope.event_id if scope else None),
            trace_id=scope.trace_id if scope else uuid.uuid4().hex[:16],
        )

    def _actual_caps(self) -> Mapping[str, set[str]]:
        # файл с фактическими возможностями пишет probe; читаем один раз, при первой надобности
        if self._caps is None:
            self._caps = load_caps(CAPS_FILE)
        return self._caps

    def check_configured(self) -> None:
        try:
            api_key, folder = self._secrets()
        except ValidationError:
            raise LLMNotConfigured("настройки сервиса не загружены: вызов модели невозможен") from None
        if not api_key or not folder:
            raise LLMNotConfigured("не заданы LLM_API_KEY и LLM_FOLDER_ID: вызов модели невозможен")

    async def budget_state(self) -> BudgetState:
        """Ступень лестницы и расход за сутки со сверкой по БД; ключ не нужен, в сеть не ходит."""
        budget = await self._ready()
        await budget.sync()
        return budget.state()

    async def provider(self, name: str) -> Provider:
        self.check_configured()
        await self._ready()
        return self._providers[name]

    async def raw_call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        reasoning_off: bool = False,
    ) -> RawAnswer:
        """Один запрос к конкретной модели для probe (ML-2).

        Без кэша, резерва модели и предохранителя: probe проверяет модель, а не задачу. Бюджет, слот
        провайдера и строка llm_calls — как у обычного вызова. BudgetExceeded поднимается наружу.
        """
        if model not in ALLOWED_MODELS:
            raise ValueError(f"модель {model!r} не входит в белый список кейса")
        self.check_configured()
        budget = await self._ready()
        await budget.sync()
        assert self._store is not None
        cfg = self.config.task("probe")
        budget.check("probe", "background", cfg.essential)
        mcfg = self.config.models[model]
        provider = self._providers[mcfg.provider]
        req = self._request("probe", cfg, "background", messages, None, None, None)
        req.tools = tools
        body: dict[str, Any] = {
            "model": provider.uri(mcfg.uri),
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "messages": messages,
        }
        if response_format is not None:
            body["response_format"] = response_format
        if tools is not None:
            body["tools"], body["tool_choice"] = tools, "auto"
        if reasoning_off:
            body["reasoning_effort"] = "none"
        started = self._clock()
        deadline = started + RAW_DEADLINE_S
        row = self._new_row(req, model)
        usage = _Usage()
        result: ChatResult | None = None
        error: ProviderError | None = None
        try:
            async with provider.slot("background", max(deadline - started - MIN_WINDOW_S, 0.001)):
                with budget.hold("probe", cfg.essential, self._reserve_rub(req, mcfg)):
                    result = await provider.chat(body, deadline=deadline)
        except ProviderError as exc:
            error = exc
        except BudgetExceeded:
            raise
        except Exception as exc:
            # неожиданный сбой клиента тоже должен оставить строку llm_calls и замаскированный текст
            log.warning("шлюз: неожиданная ошибка probe модели %s (%s)", model, type(exc).__name__)
            error = ProviderError(
                provider.mask(f"{type(exc).__name__}: {exc}")[:ERROR_LIMIT], model_fault=False
            )
        else:
            usage.tokens_in, usage.tokens_out = result.tokens_in, result.tokens_out
        if error is not None:
            row.error = str(error)[:ERROR_LIMIT]
        self._charge(row, mcfg, usage)
        await self._finish(row, started)
        return RawAnswer(result, error, row.ms, row.cost_rub)

    async def aclose(self) -> None:
        """Закрыть HTTP-клиенты провайдеров: нужно коротким процессам вроде CLI."""
        for provider in self._providers.values():
            await provider.aclose()

    def _secrets(self) -> tuple[str, str]:
        settings = self._settings or get_settings()
        return settings.llm_api_key.get_secret_value(), settings.llm_folder_id.get_secret_value()

    async def _ready(self) -> Budget:
        if self._budget is not None:
            return self._budget
        async with self._init_lock:
            if self._budget is not None:
                return self._budget
            settings = self._settings or get_settings()
            api_key, folder = self._secrets()
            for name, provider_cfg in self.config.providers.items():
                self._providers[name] = Provider(
                    name,
                    provider_cfg,
                    api_key=api_key,
                    folder=folder,
                    base_url=settings.llm_base_url or None,
                    sleep=self._sleep,
                    clock=self._clock,
                )
            if self._store is None:
                self._store = LazyStore(get_pool, clock=self._clock)
            kwargs = {"now": self._now} if self._now else {}
            self._budget = Budget(
                settings.llm_daily_budget_rub, store=self._store, clock=self._clock, **kwargs
            )
            return self._budget

    def _chain(self, cfg: TaskCfg, need_cap: str | None) -> list[str]:
        models = [m for m in [cfg.model, *cfg.fallback] if m]
        if need_cap is None:
            return models
        return [m for m in models if need_cap in self.config.caps(m, self._actual_caps())]

    def _best_mode(self, model: str) -> JsonMode:
        if model not in self._json_mode:
            caps = self.config.caps(model, self._actual_caps())
            self._json_mode[model] = (
                "schema" if "json_schema" in caps else "object" if "json_object" in caps else "prompt"
            )
        return self._json_mode[model]

    async def _run(self, req: _Request) -> BaseModel | ToolStepResult:
        # дедлайн отсчитываем с самого начала: подготовка к вызову тоже входит в бюджет времени полосы
        deadline = self._clock() + LANE_DEADLINE_S[req.lane]
        self.check_configured()
        budget = await self._ready()
        await budget.sync()
        chain = self._chain(req.cfg, req.need_cap)
        if not chain:
            raise AllModelsFailed(
                f"задача {req.task}: среди её моделей нет ни одной с возможностью {req.need_cap}"
            )
        for model in chain:
            if model not in ALLOWED_MODELS:
                raise ValueError(f"модель {model!r} не входит в белый список кейса")
        cached = await self._from_cache(req, chain)
        if cached is not None:
            return cached
        # лестница — после кэша: повтор бесплатен, и прогретый кэш выдаётся на любой ступени
        budget.check(req.task, req.lane, req.cfg.essential, priority=req.priority)
        errors: list[str] = []
        for index, model in enumerate(chain):
            now = self._clock()
            if deadline - now < MIN_WINDOW_S:
                errors.append("истёк дедлайн вызова")
                break
            last = index == len(chain) - 1
            if not self.breaker.allows(model) and not last:
                errors.append(f"{model}: предохранитель разомкнут")
                continue
            # в интерактивной полосе зависшая основная модель не должна съесть окно резерва целиком
            until = deadline if last or req.lane != "interactive" else now + (deadline - now) / 2
            try:
                return await self._attempt(req, model, until)
            except _Failed as failed:
                errors.append(f"{model}: {failed}")
        raise AllModelsFailed(f"задача {req.task}: ни одна модель не ответила. " + "; ".join(errors))

    def _new_row(self, req: _Request, model: str) -> CallRow:
        return CallRow(
            task=req.task,
            model=model,
            lane=req.lane,
            prompt_version=req.prompt_version,
            fallback_from=req.cfg.model if model != req.cfg.model else None,
            trace_id=req.trace_id,
            event_id=req.event_id,
        )

    async def _from_cache(self, req: _Request, chain: list[str]) -> BaseModel | None:
        """Повтор отдаётся из кэша любой модели цепочки, прежде чем платить основной модели заново."""
        assert self._store is not None
        if req.schema is None:
            return None
        for model in chain:
            hit = await self._store.cache_get(self._cache_key(req, self.config.models[model]))
            if hit is None:
                continue
            try:
                cached = req.schema.model_validate(hit)
            except ValidationError:
                log.info("шлюз: запись кэша не проходит текущую схему, игнорируем (%s)", req.task)
                continue
            row = self._new_row(req, model)
            row.cached = True
            await self._finish(row, self._clock())
            return cached
        return None

    def _cache_key(self, req: _Request, mcfg: ModelCfg) -> str:
        assert req.schema is not None
        payload = {
            "uri": mcfg.uri,
            "messages": [_hash_images(m) for m in req.messages],
            "schema": req.schema.model_json_schema(),
            "t": req.cfg.temperature,
            "max_tokens": req.cfg.max_tokens,
            "reasoning": req.cfg.reasoning,
            "prompt_version": req.prompt_version,
        }
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def _reserve_rub(self, req: _Request, mcfg: ModelCfg) -> float:
        tokens_in = 0
        for message in req.messages:
            text, images = _text_of(message)
            tokens_in += estimate_tokens(text) + images * IMAGE_TOKENS
        if req.schema is not None:
            tokens_in += estimate_tokens(json.dumps(req.schema.model_json_schema(), ensure_ascii=False))
        if req.tools:
            tokens_in += estimate_tokens(json.dumps(req.tools, ensure_ascii=False))
        # у части моделей рассуждения не входят в max_tokens
        tokens_out = req.cfg.max_tokens * (2 if req.cfg.reasoning else 1)
        return tokens_in * mcfg.price.in_ / 1000 + tokens_out * mcfg.price.out / 1000

    async def _attempt(self, req: _Request, model: str, deadline: float) -> BaseModel | ToolStepResult:
        assert self._budget is not None and self._store is not None
        mcfg = self.config.models[model]
        provider = self._providers[mcfg.provider]
        started = self._clock()
        row = self._new_row(req, model)
        usage = _Usage()
        wait = max(deadline - started - MIN_WINDOW_S, 0.001)
        reserve = self._reserve_rub(req, mcfg)
        try:
            try:
                # слот берём до резерва бюджета: очередь не должна раздувать резервы
                async with provider.slot(req.lane, wait):
                    with (
                        _run_hold(req.task, reserve),
                        self._budget.hold(req.task, req.cfg.essential, reserve),
                    ):
                        result = await self._guarded_query(req, model, mcfg, provider, usage, deadline)
            except SlotTimeout as exc:
                raise _Failed(str(exc), model_fault=False) from None
        except _Failed as failed:
            row.error = str(failed)[:ERROR_LIMIT]
            if failed.model_fault:
                self.breaker.failure(model)
            self._charge(row, mcfg, usage)
            await self._finish(row, started)
            raise
        self.breaker.success(model)
        self._charge(row, mcfg, usage)
        if req.schema is not None:
            await self._store.cache_put(
                self._cache_key(req, mcfg), req.task, model, result.model_dump(mode="json")
            )
        await self._finish(row, started)
        return result

    async def _guarded_query(
        self,
        req: _Request,
        model: str,
        mcfg: ModelCfg,
        provider: Provider,
        usage: _Usage,
        deadline: float,
    ) -> BaseModel | ToolStepResult:
        try:
            return await self._query_slot(req, model, mcfg, provider, usage, deadline)
        except _Failed:
            raise
        except Exception as exc:
            # неожиданный сбой клиента (цикл событий закрыт и т.п.) не должен ломать контракт LLMUnavailable
            log.warning("шлюз: неожиданная ошибка при вызове модели %s (%s)", model, type(exc).__name__)
            raise _Failed(
                provider.mask(f"{type(exc).__name__}: {exc}")[:ERROR_LIMIT], model_fault=False
            ) from None

    def _charge(self, row: CallRow, mcfg: ModelCfg, usage: _Usage) -> None:
        row.tokens_in, row.tokens_out = usage.tokens_in, usage.tokens_out
        row.cost_rub = round(
            usage.tokens_in * mcfg.price.in_ / 1000 + usage.tokens_out * mcfg.price.out / 1000, 4
        )

    async def _finish(self, row: CallRow, started: float) -> None:
        assert self._budget is not None and self._store is not None
        row.ms = int((self._clock() - started) * 1000)
        self._budget.add(row.cost_rub)
        scope = _scope.get()
        if scope is not None:
            scope.cost_rub += row.cost_rub
            scope.tokens_in += row.tokens_in
            scope.tokens_out += row.tokens_out
            scope.calls.append(
                ScopeCall(row.task, row.model, row.ms, row.cost_rub, row.cached, row.fallback_from)
            )
        await self._store.record(row)

    def _body(
        self, req: _Request, model: str, mcfg: ModelCfg, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._providers[mcfg.provider].uri(mcfg.uri),
            "temperature": req.cfg.temperature,
            "max_tokens": req.cfg.max_tokens,
        }
        if req.schema is None:
            body["tools"], body["tool_choice"] = req.tools, "auto"
        else:
            mode = self._best_mode(model)
            schema = req.schema.model_json_schema()
            if mode == "schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": req.schema.__name__, "schema": strict_schema(schema)},
                }
            else:
                if mode == "object":
                    body["response_format"] = {"type": "json_object"}
                messages = _with_instruction(messages, schema_instruction(schema))
        body["messages"] = messages
        if (
            not req.cfg.reasoning
            and mcfg.reasoning_off == "reasoning_effort"
            and model not in self._no_reasoning_param
        ):
            body["reasoning_effort"] = "none"
        return body

    async def _chat(
        self,
        req: _Request,
        model: str,
        mcfg: ModelCfg,
        provider: Provider,
        messages: list[dict[str, Any]],
        deadline: float,
    ) -> ChatResult:
        while True:
            body = self._body(req, model, mcfg, messages)
            try:
                answer = await provider.chat(body, deadline=deadline)
            except BadRequest as exc:
                reason = str(exc).lower()
                if "reasoning_effort" in body and "reasoning" in reason:
                    self._no_reasoning_param.add(model)
                    log.warning("шлюз: модель %s не принимает reasoning_effort, параметр отключён", model)
                    continue
                lower = DOWNGRADE[self._best_mode(model)] if req.schema is not None else None
                if lower and any(
                    w in reason for w in ("response_format", "json_schema", "json schema", "json_object")
                ):
                    self._json_mode[model] = lower
                    log.warning("шлюз: модель %s, режим JSON понижен до %s", model, lower)
                    continue
                raise
            if answer.finish_reason == "length":
                log.warning("шлюз: ответ модели %s оборван по max_tokens (задача %s)", model, req.task)
            return answer

    async def _query_slot(
        self,
        req: _Request,
        model: str,
        mcfg: ModelCfg,
        provider: Provider,
        usage: _Usage,
        deadline: float,
    ) -> BaseModel | ToolStepResult:
        if req.schema is None:
            return await self._tools_query(req, model, mcfg, provider, usage, deadline)
        schema_json = req.schema.model_json_schema()
        messages = list(req.messages)
        for attempt in range(VALIDATION_ATTEMPTS):
            try:
                answer = await self._chat(req, model, mcfg, provider, messages, deadline)
            except ProviderError as exc:
                raise _Failed(str(exc), model_fault=exc.model_fault) from exc
            usage.tokens_in += answer.tokens_in
            usage.tokens_out += answer.tokens_out
            if "{" not in answer.text:
                # пустой ответ или отказ текстом: тот же запрос даст то же самое за те же деньги
                raise _Failed("ответ без JSON")
            try:
                data = drop_optional_nulls(parse_json(answer.text), schema_json)
                return req.schema.model_validate(data)
            except ValueError as exc:  # JSONDecodeError и ValidationError
                problem = provider.mask(str(exc))[:ERROR_LIMIT]
                if attempt == VALIDATION_ATTEMPTS - 1:
                    raise _Failed(f"ответ не прошёл проверку: {problem}") from exc
                messages = [
                    *messages,
                    {"role": "assistant", "content": answer.text},
                    {
                        "role": "user",
                        "content": f"Ответ не прошёл проверку: {problem}\nИсправь и верни только JSON.",
                    },
                ]
        raise AssertionError("недостижимо")

    async def _tools_query(
        self,
        req: _Request,
        model: str,
        mcfg: ModelCfg,
        provider: Provider,
        usage: _Usage,
        deadline: float,
    ) -> ToolStepResult:
        try:
            answer = await self._chat(req, model, mcfg, provider, req.messages, deadline)
        except ProviderError as exc:
            raise _Failed(str(exc), model_fault=exc.model_fault) from exc
        usage.tokens_in += answer.tokens_in
        usage.tokens_out += answer.tokens_out
        if not answer.text and not answer.tool_calls:
            raise _Failed("пустой ответ модели")
        return ToolStepResult(text=answer.text or None, tool_calls=answer.tool_calls)


gw = Gateway(load_config(REPO_ROOT / "config" / "models.yaml"))
