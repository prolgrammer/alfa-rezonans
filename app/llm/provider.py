import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.llm.config import ProviderCfg
from app.llm.types import Lane, ToolCallRequest

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.5
MAX_ATTEMPTS = 3
BACKOFF_S = (1.0, 2.0, 4.0)
RETRY_AFTER_CAP_S = 10.0
MIN_ATTEMPT_WINDOW_S = 3.0
ERROR_TEXT_LIMIT = 300

_AUTH_RE = re.compile(r"(Api-Key|Bearer)\s+[A-Za-z0-9_\-.]{8,}")
_MODEL_URI_RE = re.compile(r"\b(gpt|emb|cls)://[^/\s]+/")


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN)) if text else 0


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None, *, model_fault: bool = True) -> None:
        super().__init__(message)
        self.status = status
        # False: сбой не по вине модели (дедлайн, непринятый запрос): предохранитель его не считает
        self.model_fault = model_fault


class BadRequest(ProviderError):
    """Ответ 400: текст уже замаскирован, шлюз по нему решает, какой параметр понизить."""


class SlotTimeout(ProviderError):
    """Не дождались свободного слота провайдера до конца дедлайна."""


class Transient(ProviderError):
    """Сеть, таймаут, 429 или 5xx после исчерпания повторов."""


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str | None = None
    # модель рассуждала: текст рассуждений в ответе или их токены в usage (нужно probe)
    reasoning: bool = False


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


class Provider:
    def __init__(
        self,
        name: str,
        cfg: ProviderCfg,
        *,
        api_key: str,
        folder: str,
        base_url: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.cfg = cfg
        self._api_key = api_key
        self._folder = folder
        self._sleep = sleep
        self._clock = clock
        headers = {"Authorization": f"Api-Key {api_key}", "x-folder-id": folder, **cfg.headers}
        self._client = httpx.AsyncClient(base_url=(base_url or cfg.base_url).rstrip("/"), headers=headers)
        k = min(cfg.interactive_slots, cfg.concurrency - 1)
        self._background = asyncio.Semaphore(cfg.concurrency - k)
        self._interactive = asyncio.Semaphore(k) if k else self._background

    def uri(self, template: str) -> str:
        return template.replace("{folder}", self._folder)

    @contextlib.asynccontextmanager
    async def slot(self, lane: Lane, wait_s: float) -> AsyncIterator[None]:
        """Слот провайдера на время запроса; SlotTimeout, если не дождались.

        Интерактивный вызов берёт свой зарезервированный слот, а если он занят, свободный фоновый:
        резерв защищает интерактив от фона, а не ограничивает его.
        """
        semaphore = await self._acquire(lane, wait_s)
        try:
            yield
        finally:
            semaphore.release()

    async def _acquire(self, lane: Lane, wait_s: float) -> asyncio.Semaphore:
        try:
            return await self._take(lane, wait_s)
        except TimeoutError:
            raise SlotTimeout(
                "нет свободного слота у провайдера до конца дедлайна", model_fault=False
            ) from None

    async def _take(self, lane: Lane, wait_s: float) -> asyncio.Semaphore:
        if lane == "interactive" and self._interactive is not self._background:
            for semaphore in (self._interactive, self._background):
                if not semaphore.locked():
                    await semaphore.acquire()  # свободный семафор отдаёт слот без ожидания
                    return semaphore
            await asyncio.wait_for(self._interactive.acquire(), wait_s)
            return self._interactive
        await asyncio.wait_for(self._background.acquire(), wait_s)
        return self._background

    def mask(self, text: str) -> str:
        for secret in (self._api_key, self._folder):
            if secret and len(secret) >= 6:
                text = text.replace(secret, "***")
        text = _MODEL_URI_RE.sub(r"\1://***/", text)
        return _AUTH_RE.sub(r"\1 ***", text)

    async def chat(self, body: dict[str, Any], *, deadline: float) -> ChatResult:
        last = ""
        status: int | None = None
        for attempt in range(MAX_ATTEMPTS):
            remaining = deadline - self._clock()
            if remaining < MIN_ATTEMPT_WINDOW_S:
                raise Transient(
                    f"до дедлайна вызова осталось меньше {MIN_ATTEMPT_WINDOW_S:.0f} с. {last}",
                    status,
                    model_fault=False,
                )
            pause = BACKOFF_S[attempt]
            limit = min(self.cfg.timeout_s, remaining)
            try:
                # httpx ограничивает каждую операцию сокета отдельно, а не запрос целиком:
                # без внешнего таймаута медленный поток байтов растянул бы вызов за дедлайн
                async with asyncio.timeout(limit):
                    response = await self._client.post(
                        "/chat/completions",
                        json=body,
                        timeout=httpx.Timeout(limit, connect=min(5.0, limit)),
                    )
            except TimeoutError:
                last, status = f"таймаут запроса {limit:.0f} с", None
            except httpx.RequestError as exc:
                last, status = self.mask(f"{type(exc).__name__}: {exc}")[:ERROR_TEXT_LIMIT], None
            else:
                status = response.status_code
                if status == 200:
                    try:
                        return self._parse(body, response.json())
                    except (ValueError, KeyError, TypeError, IndexError) as exc:
                        last = f"ответ провайдера не разобран: {type(exc).__name__}"
                else:
                    text = self.mask(response.text)[:ERROR_TEXT_LIMIT]
                    if status == 400:
                        raise BadRequest(f"HTTP 400: {text}", 400, model_fault=False)
                    if status != 429 and status < 500:
                        raise ProviderError(f"HTTP {status}: {text}", status)
                    last = f"HTTP {status}: {text}"
                    if status == 429:
                        pause = self._retry_after(response, pause)
            if attempt == MAX_ATTEMPTS - 1 or deadline - self._clock() - pause < MIN_ATTEMPT_WINDOW_S:
                break
            await self._sleep(pause)
        raise Transient(last, status)

    async def list_models(self) -> list[str] | None:
        """Идентификаторы моделей из GET /models; None, если провайдер список не отдаёт."""
        try:
            response = await self._client.get("/models", timeout=httpx.Timeout(15.0, connect=5.0))
        except httpx.RequestError:
            return None
        if response.status_code != 200:
            return None
        try:
            data = response.json().get("data")
        except (ValueError, AttributeError):
            return None
        if not isinstance(data, list):
            return None
        return [str(m["id"]) for m in data if isinstance(m, dict) and m.get("id")]

    @staticmethod
    def _retry_after(response: httpx.Response, default: float) -> float:
        try:
            return min(float(response.headers["Retry-After"]), RETRY_AFTER_CAP_S)
        except (KeyError, ValueError):
            return default

    def _parse(self, body: dict[str, Any], data: dict[str, Any]) -> ChatResult:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = None
            if not isinstance(args, dict):
                log.warning("аргументы вызова функции %r не разобраны", function.get("name"))
                args = {}
            calls.append(
                ToolCallRequest(id=str(raw.get("id") or ""), name=function.get("name") or "", args=args)
            )
        usage = data.get("usage")
        if usage and usage.get("prompt_tokens") is not None:
            tokens_in, tokens_out = usage["prompt_tokens"], usage.get("completion_tokens") or 0
        else:
            tokens_in = sum(estimate_tokens(_message_text(m)) for m in body.get("messages", []))
            produced = text + "".join(json.dumps(c.args, ensure_ascii=False) for c in calls)
            tokens_out = estimate_tokens(produced)
        details = (usage or {}).get("completion_tokens_details") or {}
        reasoning = bool(
            message.get("reasoning_content") or message.get("reasoning") or details.get("reasoning_tokens")
        )
        return ChatResult(text, calls, tokens_in, tokens_out, choice.get("finish_reason"), reasoning)
