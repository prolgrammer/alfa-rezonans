from fastapi import APIRouter, Depends

from app.api.deps import BackendDep, Expert, require_csrf
from app.api.errors import error_responses
from app.api.schemas import LlmUsage, Quality, SourceHealth, ThesisVerify, ToolCallLog
from app.contracts import ThesisItem

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_csrf)],
    responses=error_responses(401, 403, 422, 503),
)


@router.get("/quality", response_model=Quality, summary="Метрики качества")
async def quality(_: Expert, backend: BackendDep) -> Quality:
    return await backend.quality()


@router.get("/llm/usage", response_model=LlmUsage, summary="Расходы на модели за сутки")
async def llm_usage(_: Expert, backend: BackendDep) -> LlmUsage:
    return await backend.llm_usage()


@router.get("/tool-calls", response_model=list[ToolCallLog], summary="Журнал вызовов инструментов")
async def tool_calls(_: Expert, backend: BackendDep) -> list[ToolCallLog]:
    return await backend.tool_calls()


@router.get("/sources", response_model=list[SourceHealth], summary="Здоровье сбора новостей")
async def sources(_: Expert, backend: BackendDep) -> list[SourceHealth]:
    return await backend.sources_health()


@router.get("/theses", response_model=list[ThesisItem], summary="Тезисы банка для вычитки")
async def theses(_: Expert, backend: BackendDep) -> list[ThesisItem]:
    return await backend.theses()


@router.patch(
    "/theses/{thesis_id}",
    response_model=ThesisItem,
    responses=error_responses(404),
    summary="Отметить тезис верным или неверным",
)
async def verify_thesis(thesis_id: int, body: ThesisVerify, _: Expert, backend: BackendDep) -> ThesisItem:
    return await backend.verify_thesis(thesis_id, body.verified)
