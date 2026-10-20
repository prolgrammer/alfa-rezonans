from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.backend import Backend
from app.api.deps import MockSession
from app.api.errors import ApiError, install_handlers
from app.api.live import LiveBackend
from app.api.mock import MockBackend
from app.api.routes import (
    admin,
    auth,
    bank,
    chat,
    compare,
    events,
    feed,
    feedback,
    insights,
    market,
    news,
    profile,
)
from app.api.sse import sse_component_schemas
from app.settings import Settings, get_settings

DESCRIPTION = (
    "Компания и роль берутся только из сессии. Вход — httpOnly-cookie `orientir_session`; изменяющие запросы "
    "требуют заголовок `X-CSRF-Token` (значение отдаёт `/api/me`). Ошибки: "
    '`{"error": {"code", "message", "details"}}`.'
)


def _add_sse_schemas(app: FastAPI) -> None:
    original = app.openapi

    def openapi() -> dict:
        if app.openapi_schema is None:
            schema = original()
            schema["components"]["schemas"].update(sse_component_schemas())
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def _serve_web(app: FastAPI, settings: Settings) -> None:
    dist = settings.web_dist.resolve()
    if not dist.is_dir():
        return

    # Маршрут последний: любой неизвестный путь вне /api отдаёт index.html, дальше маршрутизирует SPA.
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise ApiError(404, "not_found", "Такого маршрута нет")
        target = (dist / path).resolve()
        if target.is_file() and target.is_relative_to(dist):
            return FileResponse(target)
        return FileResponse(dist / "index.html")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Ориентир API",
        version="0.1.0",
        description=DESCRIPTION,
    )
    app.state.settings = settings
    backend: Backend = MockBackend() if settings.mock else LiveBackend()
    app.state.backend = backend
    app.state.mock_session = MockSession.initial()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.cors_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Last-Event-ID"],
    )
    install_handlers(app)

    api = APIRouter(prefix="/api")
    for module in (auth, feed, events, insights, chat, news, compare, market, bank, feedback, profile, admin):
        api.include_router(module.router)
    app.include_router(api)

    _add_sse_schemas(app)
    _serve_web(app, settings)
    return app


app = create_app()
