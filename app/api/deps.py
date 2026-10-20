import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from app.api.backend import Backend
from app.api.errors import ApiError
from app.contracts import Access, SessionCtx, UserRole

SESSION_COOKIE = "orientir_session"
CSRF_HEADER = "X-CSRF-Token"
MOCK_CSRF_TOKEN = "mock-csrf-token"
MUTATING = frozenset({"POST", "PATCH", "PUT", "DELETE"})


@dataclass
class MockSession:
    """Единственная сессия MOCK-режима: живёт в памяти процесса, смена клиента меняет её для всех."""

    ctx: SessionCtx
    login: str = "expert"
    csrf_token: str = MOCK_CSRF_TOKEN

    @classmethod
    def initial(cls) -> "MockSession":
        ctx = SessionCtx(user_id=1, company_id="C-DEV-01", user_role=UserRole.cfo, access=Access.expert)
        return cls(ctx=ctx)


def get_backend(request: Request) -> Backend:
    return request.app.state.backend


def get_mock_session(request: Request) -> MockSession:
    if not request.app.state.settings.mock:
        raise ApiError(401, "unauthorized", "Вход ещё не реализован (BE-9)")
    return request.app.state.mock_session


MockSessionDep = Annotated[MockSession, Depends(get_mock_session)]


def get_session(session: MockSessionDep) -> SessionCtx:
    return session.ctx


Session = Annotated[SessionCtx, Depends(get_session)]


def require_access(*allowed: Access) -> Callable[[SessionCtx], Awaitable[SessionCtx]]:
    async def dependency(ctx: Session) -> SessionCtx:
        if ctx.access not in allowed:
            raise ApiError(403, "forbidden", "Недостаточно прав для этого действия")
        return ctx

    return dependency


async def require_csrf(request: Request) -> None:
    if request.method not in MUTATING:
        return
    session = get_mock_session(request)
    sent = request.headers.get(CSRF_HEADER, "")
    if not secrets.compare_digest(sent.encode(), session.csrf_token.encode()):
        raise ApiError(403, "forbidden", "Нет или неверен заголовок X-CSRF-Token")


Expert = Annotated[SessionCtx, Depends(require_access(Access.expert, Access.admin))]
BackendDep = Annotated[Backend, Depends(get_backend)]
