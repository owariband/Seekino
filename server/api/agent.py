from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import asdict
from typing import AsyncIterator

from fastapi import Header, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config.sse import SSEConfig
from ..constants.workspace import DEFAULT_LOCAL_WORKSPACE_ID
from ..repository.agent import AgentMessageState
from ..service.agent import AgentConversationService
from .context import get_services, get_sse_config
from .schema import (
    AgentConversationCreateRequest,
    AgentConversationView,
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


def send_agent_message(
    request: Request,
    body: AgentMessageSendRequest,
    conversation_id: str = Query(alias="conversationId"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> AgentMessageSendView:
    result = get_services(request).agent.send_message(
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
    service: AgentConversationService,
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
