from fastapi import APIRouter, Depends, Request

from app.api.deps import BackendDep, Session, require_csrf
from app.api.errors import error_responses
from app.api.schemas import ChatRequest
from app.api.sse import CHAT_STREAM_MODELS, EventStreamResponse, last_event_id, sse_docs, sse_response

router = APIRouter(tags=["chat"], dependencies=[Depends(require_csrf)], responses=error_responses(401, 422))


@router.post(
    "/chat",
    response_class=EventStreamResponse,
    responses={
        **sse_docs(
            CHAT_STREAM_MODELS,
            "Поток SSE: `step` (AgentStep) …, `answer` (ChatAnswer), `done`; при сбое `error` и `done`. "
            "chat_id задаёт клиент; история в MOCK не хранится.",
        ),
        **error_responses(403, 404),
    },
    summary="Вопрос по событию или ленте",
)
async def chat(body: ChatRequest, request: Request, ctx: Session, backend: BackendDep):
    last_id = last_event_id(request)
    events = await backend.chat(ctx, body, last_id or 0)
    return await sse_response(events, last_id)
