from __future__ import annotations

import asyncio
import hashlib
import time
from queue import Empty
from typing import AsyncIterator

from fastapi import Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config.sse import SSEConfig
from ..model.dto.agentDTO import (
    AgentConversationCreateRequest,
    AgentMessageSendRequest,
    AgentMessageState,
)
from ..service.agent_chat import AgentChatService, AgentMessageExecution
from ..service.media import MediaService
from ..utils.time import utc_now_naive
from ..model.vo.commonVO import Page
from .search import search_result_view
from .context import get_services, get_sse_config
from ..model.vo.agentVO import (
    AgentConversationView,
    AgentAnswerStartedView,
    AgentAnswerEvidenceView,
    AgentAnswerDeltaView,
    AgentAnswerCompletedView,
    AgentAnswerFailedView,
    AgentAnswerProgressView,
    AgentAnswerInterruptedView,
    AgentMessageEventView,
    AgentMessageSendView,
    AgentMessageView,
)


def create_agent_conversation(
    request: Request,
    body: AgentConversationCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> AgentConversationView:
    state = get_services(request).agent.create_conversation(
        workspace_id=body.workspace_id,
        title=body.title,
        idempotency_key=idempotency_key,
    )
    return AgentConversationView.model_validate(state)


def list_agent_conversations(
    request: Request,
    workspace_id: str = Query(alias="workspaceId"),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
    series_id: str | None = Query(None, alias="seriesId"),
    query: str | None = Query(None),
) -> Page[AgentConversationView]:
    page = get_services(request).agent.page_conversations(
        workspace_id=workspace_id, limit=limit, cursor=cursor, series_id=series_id, query=query,
    )
    return Page[AgentConversationView](
        items=[AgentConversationView.model_validate(item) for item in page.items],
        total=page.total, next_cursor=page.next_cursor, has_more=page.has_more,
    )


def get_agent_conversation(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    workspace_id: str = Query(alias="workspaceId"),
) -> AgentConversationView:
    state = get_services(request).agent.get_conversation(
        conversation_id,
        workspace_id=workspace_id,
    )
    return AgentConversationView.model_validate(state)


def list_agent_messages(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    workspace_id: str = Query(alias="workspaceId"),
    after_sequence_no: int = Query(-1, ge=-1, alias="afterSequenceNo"),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
) -> Page[AgentMessageView]:
    services = get_services(request)
    page = services.agent.page_messages(conversation_id, workspace_id=workspace_id,
                                       after_sequence_no=after_sequence_no, limit=limit, cursor=cursor)
    return Page[AgentMessageView](
        items=[_agent_message_view(item, media=services.media, workspace_id=workspace_id) for item in page.items],
        total=page.total, next_cursor=page.next_cursor, has_more=page.has_more,
    )


def get_agent_message(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    workspace_id: str = Query(alias="workspaceId"),
    message_id: str | None = Query(None, alias="messageId"),
    idempotency_key: str | None = Query(None, alias="idempotencyKey"),
) -> AgentMessageSendView:
    services = get_services(request)
    pair = services.agent.get_message(conversation_id, workspace_id=workspace_id,
                                     message_id=message_id, idempotency_key=idempotency_key)
    return AgentMessageSendView(
        user_message=_agent_message_view(pair.user_message, media=services.media, workspace_id=workspace_id),
        assistant_message=_agent_message_view(pair.assistant_message, media=services.media, workspace_id=workspace_id),
    )


async def send_agent_message(
    request: Request,
    body: AgentMessageSendRequest,
    conversation_id: str = Query(alias="conversationId"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> AgentMessageSendView | StreamingResponse:
    service = get_services(request).agent
    accepted = []
    for item in request.headers.get("accept", "").split(","):
        media_type, *parameters = item.lower().split(";")
        media_type = media_type.strip()
        if media_type not in {"application/json", "text/event-stream"}:
            continue
        quality = 1.0
        for parameter in parameters:
            name, _, value = parameter.strip().partition("=")
            if name == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0
        if 0 < quality <= 1:
            accepted.append((quality, media_type))
    wants_stream = max(
        accepted, key=lambda item: item[0], default=(0, "application/json"),
    )[1] == "text/event-stream"
    if wants_stream:
        execution = await run_in_threadpool(
            service.start_message,
            conversation_id,
            workspace_id=body.workspace_id,
            content=body.content,
            series_ids=body.series_ids,
            reference=body.reference.model_dump(by_alias=True) if body.reference is not None else None,
            idempotency_key=idempotency_key,
            top_k=body.top_k,
            stream=True,
        )
        return StreamingResponse(
            _answer_events(request, execution, get_sse_config(request),
                           media=get_services(request).media, workspace_id=body.workspace_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result = await run_in_threadpool(
        service.send_message,
        conversation_id,
        workspace_id=body.workspace_id,
        content=body.content,
        series_ids=body.series_ids,
        reference=body.reference.model_dump(by_alias=True) if body.reference is not None else None,
        idempotency_key=idempotency_key,
        top_k=body.top_k,
    )
    return AgentMessageSendView(
        user_message=_agent_message_view(result.user_message, media=get_services(request).media, workspace_id=body.workspace_id),
        assistant_message=_agent_message_view(result.assistant_message, media=get_services(request).media, workspace_id=body.workspace_id),
    )


async def stream_agent_messages(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    workspace_id: str = Query(alias="workspaceId"),
) -> StreamingResponse:
    service = get_services(request).agent
    await run_in_threadpool(
        service.get_conversation,
        conversation_id,
        workspace_id=workspace_id,
    )
    return StreamingResponse(
        _agent_message_events(
            request,
            service,
            conversation_id,
            workspace_id,
            get_sse_config(request),
            media=get_services(request).media,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _agent_message_events(
    request: Request,
    service: AgentChatService,
    conversation_id: str,
    workspace_id: str,
    config: SSEConfig,
    *, media: MediaService | None = None,
) -> AsyncIterator[str]:
    previous_fingerprint: str | None = None
    last_event_at = time.monotonic()
    while True:
        states = await run_in_threadpool(
            service.list_all_messages,
            conversation_id,
            workspace_id=workspace_id,
        )
        view = AgentMessageEventView(
            conversation_id=conversation_id,
            messages=[_agent_message_view(item, media=media, workspace_id=workspace_id) for item in states],
            occurred_at=utc_now_naive(),
        )
        payload = view.model_dump_json(by_alias=True)
        fingerprint = hashlib.sha256(view.model_dump_json(by_alias=True, exclude={"occurred_at"}).encode("utf-8")).hexdigest()
        if fingerprint != previous_fingerprint:
            last_event_at = time.monotonic()
            yield f"event: messages\ndata: {payload}\n\n"
            previous_fingerprint = fingerprint
        elif time.monotonic() - last_event_at >= config.keepalive_interval_seconds:
            last_event_at = time.monotonic()
            yield ": keep-alive\n\n"
        if await request.is_disconnected():
            return
        await asyncio.sleep(config.poll_interval_seconds)


def _agent_message_view(
    state: AgentMessageState | dict, *, media: MediaService | None = None,
    workspace_id: str | None = None,
) -> AgentMessageView:
    state = AgentMessageState(**state) if isinstance(state, dict) else state
    result = None
    if state.search_result is not None:
        if media is None or workspace_id is None:
            raise ValueError("evidence projection requires the owning Workspace and media service")
        result = search_result_view(state.search_result, media, workspace_id=workspace_id)
    return AgentMessageView(
        message_id=state.message_id, conversation_id=state.conversation_id,
        series_ids=list(state.series_ids), sequence_no=state.sequence_no,
        role=state.role, status=state.status, phase=state.phase, version=state.version,
        reference=state.reference, content=state.content, model_name=state.model_name,
        reply_to_message_id=state.reply_to_message_id, retrieval_query=state.retrieval_query,
        search_result=result, last_error=state.last_error, created_at=state.created_at,
        updated_at=state.updated_at,
    )


_ANSWER_EVENT_MODELS = {
    "answer.started": AgentAnswerStartedView,
    "answer.evidence": AgentAnswerEvidenceView,
    "answer.delta": AgentAnswerDeltaView,
    "answer.completed": AgentAnswerCompletedView,
    "answer.failed": AgentAnswerFailedView,
    "answer.progress": AgentAnswerProgressView,
    "answer.interrupted": AgentAnswerInterruptedView,
}


async def _answer_events(
    request: Request,
    execution: AgentMessageExecution,
    config: SSEConfig,
    *, media: MediaService | None = None, workspace_id: str | None = None,
) -> AsyncIterator[str]:
    last_event_at = time.monotonic()
    try:
        while not await request.is_disconnected():
            try:
                event = await run_in_threadpool(
                    execution.next_event,
                    min(1, config.keepalive_interval_seconds),
                )
            except Empty:
                if time.monotonic() - last_event_at >= config.keepalive_interval_seconds:
                    last_event_at = time.monotonic()
                    yield ": keep-alive\n\n"
                continue
            if event is None:
                return
            event_type = event.get("type")
            model = _ANSWER_EVENT_MODELS.get(event_type)
            if model is None:
                continue
            data = dict(event)
            for field in ("userMessage", "assistantMessage"):
                if field in data:
                    data[field] = _agent_message_view(data[field], media=media, workspace_id=workspace_id)
            if event_type == "answer.evidence":
                if media is None or workspace_id is None:
                    raise ValueError("evidence projection requires the owning Workspace and media service")
                data["searchResult"] = search_result_view(data["searchResult"], media, workspace_id=workspace_id)
            view = model.model_validate(data)
            last_event_at = time.monotonic()
            payload = view.model_dump_json(
                by_alias=True,
                exclude={name for name, value in view if value is None},
            )
            yield f"event: {view.type}\nid: {view.message_id}:{view.sequence}\ndata: {payload}\n\n"
    finally:
        execution.detach()
