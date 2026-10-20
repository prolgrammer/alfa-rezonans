from fastapi import APIRouter, Depends

from app.api.deps import BackendDep, Session, require_csrf
from app.api.errors import error_responses
from app.api.schemas import Profile, ProfilePatch, ProfileText, ProfileTextPreview

router = APIRouter(
    prefix="/profile",
    tags=["profile"],
    dependencies=[Depends(require_csrf)],
    responses=error_responses(401, 422),
)


@router.get("", response_model=Profile, responses=error_responses(404), summary="Профиль компании")
async def get_profile(ctx: Session, backend: BackendDep) -> Profile:
    return await backend.profile(ctx)


@router.patch(
    "",
    response_model=Profile,
    responses=error_responses(403, 503),
    summary="Поправить признак профиля",
)
async def patch_profile(body: ProfilePatch, ctx: Session, backend: BackendDep) -> Profile:
    return await backend.patch_profile(ctx, body)


@router.post(
    "/text",
    response_model=ProfileTextPreview,
    responses=error_responses(403, 503),
    summary="Описать бизнес словами: чувствительности до и после",
)
async def profile_text(body: ProfileText, ctx: Session, backend: BackendDep) -> ProfileTextPreview:
    return await backend.profile_text(ctx, body)
