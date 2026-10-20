import secrets

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import (
    SESSION_COOKIE,
    BackendDep,
    Expert,
    MockSession,
    MockSessionDep,
    require_csrf,
)
from app.api.errors import ApiError, error_responses
from app.api.schemas import DemoSwitch, Health, LoginRequest, Me, Ok

router = APIRouter(tags=["auth"])


async def _me(session: MockSession, backend: BackendDep) -> Me:
    ctx = session.ctx
    return Me(
        user_id=ctx.user_id,
        login=session.login,
        company_id=ctx.company_id,
        company_name=await backend.company_name(ctx),
        user_role=ctx.user_role,
        access=ctx.access,
        csrf_token=session.csrf_token,
    )


@router.get("/health", response_model=Health)
async def health(request: Request) -> Health:
    return Health(status="ok", mock=request.app.state.settings.mock)


@router.post(
    "/auth/login",
    response_model=Me,
    responses=error_responses(401, 422, 503),
    summary="Войти",
)
async def login(body: LoginRequest, request: Request, response: Response, backend: BackendDep) -> Me:
    settings = request.app.state.settings
    if not settings.mock:
        raise ApiError(503, "not_implemented", "Вход ещё не реализован (BE-9)")
    expected = settings.demo_password.get_secret_value()
    if not secrets.compare_digest(body.password.encode(), expected.encode()):
        raise ApiError(401, "unauthorized", "Неверный логин или пароль")
    session: MockSession = request.app.state.mock_session
    session.login = body.login
    response.set_cookie(
        SESSION_COOKIE,
        secrets.token_urlsafe(32),
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return await _me(session, backend)


@router.post(
    "/auth/logout",
    response_model=Ok,
    dependencies=[Depends(require_csrf)],
    responses=error_responses(401, 403),
    summary="Выйти",
)
async def logout(response: Response) -> Ok:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return Ok(ok=True)


@router.get("/me", response_model=Me, responses=error_responses(401), summary="Кто я")
async def me(backend: BackendDep, session: MockSessionDep) -> Me:
    return await _me(session, backend)


@router.post(
    "/demo/switch",
    response_model=Me,
    dependencies=[Depends(require_csrf)],
    responses=error_responses(401, 403, 404, 422),
    summary="Выбрать клиента и роль для показа",
)
async def demo_switch(
    body: DemoSwitch,
    backend: BackendDep,
    _: Expert,
    session: MockSessionDep,
) -> Me:
    if not await backend.has_company(body.company_id):
        raise ApiError(404, "not_found", "Такого клиента нет")
    session.ctx = session.ctx.model_copy(update={"company_id": body.company_id, "user_role": body.user_role})
    return await _me(session, backend)
