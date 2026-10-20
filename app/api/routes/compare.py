from fastapi import APIRouter, Depends

from app.api.deps import BackendDep, Expert, require_csrf
from app.api.errors import error_responses
from app.api.schemas import Compare

router = APIRouter(
    tags=["compare"], dependencies=[Depends(require_csrf)], responses=error_responses(401, 422)
)


@router.get(
    "/compare",
    response_model=Compare,
    responses=error_responses(403, 404, 503),
    summary="Одно событие для двух компаний",
)
async def compare(event: int, a: str, b: str, ctx: Expert, backend: BackendDep) -> Compare:
    # Единственный маршрут, где компании приходят из запроса: сравнение — функция эксперта банка,
    # доступ к нему проверяет Expert; клиентская сессия сюда не попадает.
    return await backend.compare(ctx, event, a, b)
