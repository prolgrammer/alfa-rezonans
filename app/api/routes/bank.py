from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from app.api.deps import BackendDep, Session, require_csrf
from app.api.errors import error_responses

router = APIRouter(tags=["bank"], dependencies=[Depends(require_csrf)], responses=error_responses(401, 422))

PNG = {"image/png": {"schema": {"type": "string", "format": "binary"}}}


@router.get(
    "/bank/docs/{doc_id}/page/{page}",
    response_class=Response,
    responses={200: {"description": "Страница документа банка", "content": PNG}, **error_responses(404, 503)},
    summary="Страница материала банка картинкой",
)
async def bank_page(
    doc_id: int, page: Annotated[int, Path(ge=1)], _: Session, backend: BackendDep
) -> Response:
    return Response(await backend.bank_page(doc_id, page), media_type="image/png")
