import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import JsonValue
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.contracts._base import ContractModel
from app.llm.gateway import LLMUnavailable

log = logging.getLogger("orientir.api")

_CODE_BY_STATUS = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    503: "llm_unavailable",
}


class ErrorDetail(ContractModel):
    code: str
    message: str
    details: dict[str, JsonValue]


class ErrorBody(ContractModel):
    error: ErrorDetail


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.body = ErrorBody(error=ErrorDetail(code=code, message=message, details=details or {}))


_DESCRIPTIONS = {
    401: "Нет сессии",
    403: "Нет прав или неверный X-CSRF-Token",
    404: "Не найдено",
    422: "Некорректный запрос",
    503: "Маршрут ещё на заглушке",
}


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {s: {"model": ErrorBody, "description": _DESCRIPTIONS[s]} for s in statuses}


def error_body(exc: BaseException) -> tuple[int, ErrorBody]:
    """Единое соответствие исключения и тела ошибки: им пользуются и обработчики, и кадр `error` в SSE."""
    if isinstance(exc, ApiError):
        return exc.status, exc.body
    if isinstance(exc, LookupError):
        return 404, _body("not_found", "Не найдено")
    if isinstance(exc, LLMUnavailable):
        return 503, _body("llm_unavailable", "Модель сейчас недоступна")
    if isinstance(exc, StarletteHTTPException):
        status = exc.status_code
        code = _CODE_BY_STATUS.get(status, "invalid_request" if status < 500 else "internal_error")
        return status, _body(code, str(exc.detail))
    # причину пишем в журнал, а не клиенту: в тексте исключения могут быть внутренние детали
    log.error("необработанная ошибка", exc_info=exc)
    return 500, _body("internal_error", "Внутренняя ошибка сервиса")


def _body(code: str, message: str) -> ErrorBody:
    return ErrorBody(error=ErrorDetail(code=code, message=message, details={}))


def _respond(status: int, body: ErrorBody) -> JSONResponse:
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


def install_handlers(app: FastAPI) -> None:
    async def handle(_: Request, exc: Exception) -> JSONResponse:
        return _respond(*error_body(exc))

    async def handle_validation(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        # ctx ошибок pydantic может держать объекты исключений, которые не сериализуются в JSON
        errors: list[JsonValue] = [
            {"loc": [str(p) for p in e["loc"]], "message": e["msg"], "type": e["type"]} for e in exc.errors()
        ]
        body = ErrorBody(
            error=ErrorDetail(
                code="validation_error", message="Некорректный запрос", details={"errors": errors}
            )
        )
        return _respond(422, body)

    app.add_exception_handler(RequestValidationError, handle_validation)
    for exc_type in (ApiError, StarletteHTTPException, LookupError, LLMUnavailable, Exception):
        app.add_exception_handler(exc_type, handle)
