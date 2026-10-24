from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import (
    SESSION_COOKIE,
    AuthDep,
    AuthSessionDep,
    BackendDep,
    Expert,
    require_csrf,
    session_token,
)
from app.api.errors import ApiError, error_responses
from app.api.schemas import DemoSwitch, Health, LoginRequest, Me, Ok
from app.api.sessions import AuthSession

router = APIRouter(tags=["auth"])


async def _me(session: AuthSession, backend: BackendDep) -> Me:
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
    responses=error_responses(401, 422, 429),
    summary="Войти",
)
async def login(
    body: LoginRequest, request: Request, response: Response, backend: BackendDep, auth: AuthDep
) -> Me:
    session, token = await auth.login(body.login, body.password, session_token(request))
    ttl_hours = request.app.state.settings.session_ttl_hours
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ttl_hours * 3600,
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
async def logout(request: Request, response: Response, auth: AuthDep, session: AuthSessionDep) -> Ok:
    await auth.logout(session_token(request), session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return Ok(ok=True)


@router.get("/me", response_model=Me, responses=error_responses(401), summary="Кто я")
async def me(backend: BackendDep, session: AuthSessionDep) -> Me:
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
    request: Request,
    backend: BackendDep,
    auth: AuthDep,
    _: Expert,
    session: AuthSessionDep,
) -> Me:
    if not await backend.has_company(body.company_id):
        raise ApiError(404, "not_found", "Такого клиента нет")
    switched = await auth.switch(session_token(request), session, body.company_id, body.user_role)
    return await _me(switched, backend)
