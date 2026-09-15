from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import asdict
from queue import Empty
from typing import AsyncIterator

from fastapi import Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config.sse import SSEConfig
from ..constants.workspace import DEFAULT_LOCAL_WORKSPACE_ID
from ..repository.agent import AgentMessageState
from ..service.agent_chat import AgentChatService, AgentMessageExecution
from .context import get_services, get_sse_config
from .schema import (
    AgentConversationCreateRequest,
    AgentConversationView,
    AgentAnswerStartedView,
    AgentAnswerEvidenceView,
    AgentAnswerDeltaView,
    AgentAnswerCompletedView,
    AgentAnswerFailedView,
    AgentMessageEventView,
    AgentMessageSendRequest,
    AgentMessageSendView,
    AgentMessageView,
    AgentSessionCreateRequest,
    AgentSessionView,
)


def create_agent_session(
    request: Request,
    body: AgentSessionCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> AgentSessionView:
    state = get_services(request).agent.create_session(
        workspace_id=body.workspace_id,
        name=body.name,
        idempotency_key=idempotency_key,
    )
    return AgentSessionView.model_validate(asdict(state))


def list_agent_sessions(
    request: Request,
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
    limit: int = Query(100, ge=1, le=500),
) -> list[AgentSessionView]:
    return [
        AgentSessionView.model_validate(asdict(item))
        for item in get_services(request).agent.list_sessions(
            workspace_id=workspace_id,
            limit=limit,
        )
    ]


def get_agent_session(
    request: Request,
    session_id: str = Query(alias="sessionId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
) -> AgentSessionView:
    state = get_services(request).agent.get_session(
        session_id,
        workspace_id=workspace_id,
    )
    return AgentSessionView.model_validate(asdict(state))


def create_agent_conversation(
    request: Request,
    body: AgentConversationCreateRequest,
    session_id: str = Query(alias="sessionId"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> AgentConversationView:
    state = get_services(request).agent.create_conversation(
        session_id,
        workspace_id=body.workspace_id,
        title=body.title,
        series_ids=body.series_ids,
        idempotency_key=idempotency_key,
    )
    return AgentConversationView.model_validate(asdict(state))


def list_agent_conversations(
    request: Request,
    session_id: str = Query(alias="sessionId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
    limit: int = Query(100, ge=1, le=500),
) -> list[AgentConversationView]:
    return [
        AgentConversationView.model_validate(asdict(item))
        for item in get_services(request).agent.list_conversations(
            session_id,
            workspace_id=workspace_id,
            limit=limit,
        )
    ]


def get_agent_conversation(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
) -> AgentConversationView:
    state = get_services(request).agent.get_conversation(
        conversation_id,
        workspace_id=workspace_id,
    )
    return AgentConversationView.model_validate(asdict(state))


def list_agent_messages(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
    after_sequence_no: int = Query(-1, ge=-1, alias="afterSequenceNo"),
    limit: int = Query(200, ge=1, le=500),
) -> list[AgentMessageView]:
    return [
        _agent_message_view(item)
        for item in get_services(request).agent.list_messages(
            conversation_id,
            workspace_id=workspace_id,
            after_sequence_no=after_sequence_no,
            limit=limit,
        )
    ]


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
            idempotency_key=idempotency_key,
            top_k=body.top_k,
            stream=True,
        )
        return StreamingResponse(
            _answer_events(request, execution, get_sse_config(request)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result = await run_in_threadpool(
        service.send_message,
        conversation_id,
        workspace_id=body.workspace_id,
        content=body.content,
        idempotency_key=idempotency_key,
        top_k=body.top_k,
    )
    return AgentMessageSendView(
        user_message=_agent_message_view(result.user_message),
        assistant_message=_agent_message_view(result.assistant_message),
    )


async def stream_agent_messages(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    workspace_id: str = Query(DEFAULT_LOCAL_WORKSPACE_ID, alias="workspaceId"),
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
            messages=[_agent_message_view(item) for item in states],
        )
        payload = view.model_dump_json(by_alias=True)
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
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


def _agent_message_view(state: AgentMessageState) -> AgentMessageView:
    return AgentMessageView.model_validate(asdict(state))


_ANSWER_EVENT_MODELS = {
    "answer.started": AgentAnswerStartedView,
    "answer.evidence": AgentAnswerEvidenceView,
    "answer.delta": AgentAnswerDeltaView,
    "answer.completed": AgentAnswerCompletedView,
    "answer.failed": AgentAnswerFailedView,
}


async def _answer_events(
    request: Request,
    execution: AgentMessageExecution,
    config: SSEConfig,
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
            view = model.model_validate(event)
            last_event_at = time.monotonic()
            payload = view.model_dump_json(
                by_alias=True,
                exclude={name for name, value in view if value is None},
            )
            yield f"event: {view.type}\nid: {view.message_id}:{view.sequence}\ndata: {payload}\n\n"
    finally:
        execution.detach()
