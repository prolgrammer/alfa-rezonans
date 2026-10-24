import secrets
from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from fastapi import Depends, Request

from app.api.backend import Backend
from app.api.errors import ApiError
from app.api.sessions import AuthSession
from app.contracts import Access, SessionCtx, UserRole
from app.settings import Settings

SESSION_COOKIE = "orientir_session"
CSRF_HEADER = "X-CSRF-Token"
MOCK_CSRF_TOKEN = "mock-csrf-token"
MUTATING = frozenset({"POST", "PATCH", "PUT", "DELETE"})


class Authenticator(Protocol):
    """Хранилище сессий: в MOCK — память процесса, в боевом режиме — БД. Маршрутам режим не виден."""

    async def current(self, token: str | None) -> AuthSession: ...

    async def login(self, login: str, password: str, old_token: str | None) -> tuple[AuthSession, str]: ...

    async def logout(self, token: str | None, session: AuthSession) -> None: ...

    async def switch(
        self, token: str | None, session: AuthSession, company_id: str, user_role: UserRole
    ) -> AuthSession: ...


class MockAuth:
    """Единственная сессия MOCK-режима: живёт в памяти процесса, смена клиента меняет её для всех."""

    def __init__(self, settings: Settings) -> None:
        self.demo_password = settings.demo_password.get_secret_value()
        self.session = AuthSession(
            ctx=SessionCtx(user_id=1, company_id="C-DEV-01", user_role=UserRole.cfo, access=Access.expert),
            login="expert",
            csrf_token=MOCK_CSRF_TOKEN,
        )

    async def current(self, token: str | None) -> AuthSession:
        # Сессия в MOCK одна, но вход обязателен: так интерфейс проверяет ветку «401 -> экран входа».
        if not token:
            raise ApiError(401, "unauthorized", "Нужно войти")
        return self.session

    async def login(self, login: str, password: str, old_token: str | None) -> tuple[AuthSession, str]:
        if not secrets.compare_digest(password.encode(), self.demo_password.encode()):
            raise ApiError(401, "unauthorized", "Неверный логин или пароль")
        self.session.login = login
        return self.session, secrets.token_urlsafe(32)

    async def logout(self, token: str | None, session: AuthSession) -> None:
        return None

    async def switch(
        self, token: str | None, session: AuthSession, company_id: str, user_role: UserRole
    ) -> AuthSession:
        self.session.ctx = self.session.ctx.model_copy(
            update={"company_id": company_id, "user_role": user_role}
        )
        return self.session


def get_backend(request: Request) -> Backend:
    return request.app.state.backend


def get_auth(request: Request) -> Authenticator:
    return request.app.state.auth


AuthDep = Annotated[Authenticator, Depends(get_auth)]


def session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


async def get_auth_session(request: Request, auth: AuthDep) -> AuthSession:
    return await auth.current(session_token(request))


AuthSessionDep = Annotated[AuthSession, Depends(get_auth_session)]


def get_session(session: AuthSessionDep) -> SessionCtx:
    return session.ctx


Session = Annotated[SessionCtx, Depends(get_session)]


def require_access(*allowed: Access) -> Callable[[SessionCtx], Awaitable[SessionCtx]]:
    async def dependency(ctx: Session) -> SessionCtx:
        if ctx.access not in allowed:
            raise ApiError(403, "forbidden", "Недостаточно прав для этого действия")
        return ctx

    return dependency


async def require_csrf(request: Request, session: AuthSessionDep) -> None:
    if request.method not in MUTATING:
        return
    sent = request.headers.get(CSRF_HEADER, "")
    if not secrets.compare_digest(sent.encode(), session.csrf_token.encode()):
        raise ApiError(403, "forbidden", "Нет или неверен заголовок X-CSRF-Token")


Expert = Annotated[SessionCtx, Depends(require_access(Access.expert, Access.admin))]
BackendDep = Annotated[Backend, Depends(get_backend)]
