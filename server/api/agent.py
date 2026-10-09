from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import asdict, replace
from queue import Empty
from typing import AsyncIterator

from fastapi import Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config.sse import SSEConfig
from ..model.dto.agentDTO import (
    AgentConversationCreateRequest,
    AgentGlobalSettingsPatchRequest,
    AgentMessageSendRequest,
    AgentMessageState,
)
from ..service.agent_chat import AgentChatService, AgentMessageExecution
from ..service.access import model_query, require_admin, require_series_read, require_tenant_read
from ..error.auth import AuthError
from ..model.dto.authDTO import AuthIdentity
from ..service.media import MediaService
from ..utils.time import now_unix_time_ms
from ..model.vo.commonVO import Page
from .search import search_evidence_fields
from .context import get_identity, get_services, get_sse_config, revalidate_identity
from ..model.vo.agentVO import (
    AgentConversationView,
    AgentGlobalSettingsView,
    AgentMessageOptionsView,
    AgentAnswerStartedView,
    AgentAnswerEvidenceView,
    AgentAnswerBlockView,
    AgentAnswerDeltaView,
    AgentAnswerCompletedView,
    AgentAnswerFailedView,
    AgentAnswerProgressView,
    AgentAnswerInterruptedView,
    AgentMessageEventView,
    AgentMessageSendView,
    AgentMessageTimingView,
    AgentMessageView,
    AgentSearchEvidenceView,
    AgentMarkdownRenderBlockView,
    AgentRenderBlockView,
    AgentVideoRenderBlockView,
)
from ..repository.agent import AgentConversationNotFoundError
from ..repository.library import SeriesNotFoundError
from ..config.global_agent_settings import (
    patch_global_agent_settings,
    read_global_agent_settings,
)

_logger = logging.getLogger(__name__)


def get_agent_global_settings(request: Request) -> AgentGlobalSettingsView:
    require_admin(get_identity(request))
    config, revision = read_global_agent_settings(request.app.state.model_config_path)
    return AgentGlobalSettingsView(
        revision=revision,
        values=asdict(config),
        restart_required_fields=["maximum_concurrent_kicks"],
        active_values={
            "maximum_concurrent_kicks": get_services(request).agent.active_maximum_concurrent_kicks,
        },
    )


def patch_agent_global_settings(
    request: Request,
    body: AgentGlobalSettingsPatchRequest,
) -> AgentGlobalSettingsView:
    identity = get_identity(request)
    require_admin(identity)
    if identity.mode == "cloud" and "execution_timeout_seconds" in body.values:
        timeout = body.values["execution_timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)):
            raise ValueError("执行超时必须是正数")
        if timeout >= get_services(request).auth.config.query_lease_seconds:
            raise ValueError("执行超时必须小于查询并发租约有效期")
    client_host = request.client.host if request.client else None
    if identity.mode == "local" and client_host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="全局设置只允许从本机修改")
    config, revision = patch_global_agent_settings(
        body.values, body.revision, request.app.state.model_config_path,
    )
    return AgentGlobalSettingsView(
        revision=revision,
        values=asdict(config),
        restart_required_fields=["maximum_concurrent_kicks"],
        active_values={
            "maximum_concurrent_kicks": get_services(request).agent.active_maximum_concurrent_kicks,
        },
    )


def get_agent_message_options(request: Request) -> AgentMessageOptionsView:
    services = get_services(request)
    identity = get_identity(request)
    options = services.agent.message_options()
    if identity.mode == "cloud" and identity.is_guest:
        cap = services.auth.config.guest_max_model_calls
        def bounded(option, maximum):
            ceiling = max(option.minimum, min(option.maximum, maximum))
            return replace(option, default_value=min(option.default_value, ceiling), maximum=ceiling)
        options = replace(
            options,
            max_model_calls_per_turn=bounded(options.max_model_calls_per_turn, cap),
            max_tool_calls_per_turn=bounded(options.max_tool_calls_per_turn, cap * 2),
        )
    return AgentMessageOptionsView.model_validate(options)


def create_agent_conversation(
    request: Request,
    body: AgentConversationCreateRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> AgentConversationView:
    identity = get_identity(request)
    require_tenant_read(identity, body.tenant_id)
    state = get_services(request).agent.create_conversation(
        tenant_id=body.tenant_id,
        owner_user_id=identity.user_id,
        title=body.title,
        idempotency_key=idempotency_key,
    )
    return AgentConversationView.model_validate(state)


def list_agent_conversations(
    request: Request,
    tenant_id: str = Query(alias="tenantId"),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
    series_id: str | None = Query(None, alias="seriesId"),
    query: str | None = Query(None),
) -> Page[AgentConversationView]:
    identity = get_identity(request)
    require_tenant_read(identity, tenant_id)
    page = get_services(request).agent.page_conversations(
        tenant_id=tenant_id, limit=limit, cursor=cursor, series_id=series_id, query=query,
        owner_user_id=identity.user_id,
    )
    return Page[AgentConversationView](
        items=[AgentConversationView.model_validate(item) for item in page.items],
        total=page.total, next_cursor=page.next_cursor, has_more=page.has_more,
    )


def get_agent_conversation(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    tenant_id: str = Query(alias="tenantId"),
) -> AgentConversationView:
    identity = get_identity(request)
    require_tenant_read(identity, tenant_id)
    state = get_services(request).agent.get_conversation(
        conversation_id,
        tenant_id=tenant_id,
        owner_user_id=identity.user_id,
    )
    return AgentConversationView.model_validate(state)


def delete_agent_conversation(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    tenant_id: str = Query(alias="tenantId"),
) -> AgentConversationView:
    identity = get_identity(request)
    require_tenant_read(identity, tenant_id)
    state = get_services(request).agent.delete_conversation(
        conversation_id,
        tenant_id=tenant_id,
        owner_user_id=identity.user_id,
    )
    return AgentConversationView.model_validate(state)


def list_agent_messages(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    tenant_id: str = Query(alias="tenantId"),
    after_sequence_no: int = Query(-1, ge=-1, alias="afterSequenceNo"),
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None),
) -> Page[AgentMessageView]:
    identity = get_identity(request)
    require_tenant_read(identity, tenant_id)
    services = get_services(request)
    page = services.agent.page_messages(conversation_id, tenant_id=tenant_id,
                                       after_sequence_no=after_sequence_no, limit=limit, cursor=cursor,
                                       owner_user_id=identity.user_id)
    return Page[AgentMessageView](
        items=[_agent_message_view(item, media=services.media, tenant_id=tenant_id, identity=identity) for item in page.items],
        total=page.total, next_cursor=page.next_cursor, has_more=page.has_more,
    )


def get_agent_message(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    tenant_id: str = Query(alias="tenantId"),
    message_id: str | None = Query(None, alias="messageId"),
    idempotency_key: str | None = Query(None, alias="idempotencyKey"),
) -> AgentMessageSendView:
    identity = get_identity(request)
    require_tenant_read(identity, tenant_id)
    services = get_services(request)
    pair = services.agent.get_message(conversation_id, tenant_id=tenant_id,
                                     message_id=message_id, idempotency_key=idempotency_key,
                                     owner_user_id=identity.user_id)
    return AgentMessageSendView(
        user_message=_agent_message_view(pair.user_message, media=services.media, tenant_id=tenant_id, identity=identity),
        assistant_message=_agent_message_view(pair.assistant_message, media=services.media, tenant_id=tenant_id, identity=identity),
    )


async def send_agent_message(
    request: Request,
    body: AgentMessageSendRequest,
    conversation_id: str = Query(alias="conversationId"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> AgentMessageSendView | StreamingResponse:
    services = get_services(request)
    service = services.agent
    identity = await run_in_threadpool(get_identity, request)
    require_tenant_read(identity, body.tenant_id)
    source_tenant_id = body.source_tenant_id or body.tenant_id
    series_ids = await run_in_threadpool(
        _authorize_query_scope, request, identity, source_tenant_id, body.series_ids,
    )
    playback = (body.extra_context or {}).get("playback")
    if identity.mode == "cloud" and isinstance(playback, dict):
        video = await run_in_threadpool(
            services.library.get_video, playback.get("videoId"), tenant_id=source_tenant_id,
        )
        await run_in_threadpool(
            _authorize_query_scope, request, identity, source_tenant_id, [video.series_id],
        )
    await run_in_threadpool(
        service.get_conversation, conversation_id, tenant_id=body.tenant_id,
        owner_user_id=identity.user_id,
    )
    max_model_calls = body.max_model_calls_per_turn
    max_tool_calls = body.max_tool_calls_per_turn
    if identity.mode == "cloud" and identity.is_guest:
        options = service.message_options()
        config = services.auth.config
        max_model_calls = min(
            max_model_calls or options.max_model_calls_per_turn.default_value,
            config.guest_max_model_calls,
        )
        max_tool_calls = min(
            max_tool_calls or options.max_tool_calls_per_turn.default_value,
            config.guest_max_model_calls * 2,
        )
    arguments = dict(
        tenant_id=body.tenant_id, source_tenant_id=source_tenant_id,
        owner_user_id=identity.user_id, content=body.content, series_ids=series_ids,
        reference=body.reference.model_dump(by_alias=True) if body.reference is not None else None,
        extra_context=body.extra_context, idempotency_key=idempotency_key, top_k=body.top_k,
        max_model_calls_per_turn=max_model_calls, max_tool_calls_per_turn=max_tool_calls,
        max_loop_turns=None if identity.mode == "cloud" else body.max_loop_turns,
    )
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
        reservation = model_query(getattr(services, "auth", None), identity,
                                  request.client.host if request.client else "unknown",
                                  maximum_concurrent=getattr(service, "active_maximum_concurrent_kicks", None))
        await run_in_threadpool(reservation.__enter__)
        try:
            execution = await run_in_threadpool(service.start_message, conversation_id, stream=True, **arguments)
        except BaseException:
            await run_in_threadpool(reservation.__exit__, None, None, None)
            raise
        # Disconnected HTTP consumers do not stop the Service-owned execution.
        # Keep the concurrency reservation until the actual model work finishes.
        if execution.future is not None:
            await run_in_threadpool(
                execution.future.add_done_callback,
                lambda _future: reservation.__exit__(None, None, None),
            )
        else:
            await run_in_threadpool(reservation.__exit__, None, None, None)
        return StreamingResponse(
            _answer_events(request, execution, get_sse_config(request),
                           media=services.media, tenant_id=source_tenant_id, series_ids=series_ids),
            media_type="text/event-stream",
            headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
        )
    def send():
        with model_query(getattr(services, "auth", None), identity,
                         request.client.host if request.client else "unknown",
                         maximum_concurrent=getattr(service, "active_maximum_concurrent_kicks", None)):
            return service.send_message(conversation_id, **arguments)
    result = await run_in_threadpool(send)
    identity = await run_in_threadpool(revalidate_identity, request)
    return AgentMessageSendView(
        user_message=_agent_message_view(result.user_message, media=services.media, tenant_id=source_tenant_id, identity=identity),
        assistant_message=_agent_message_view(result.assistant_message, media=services.media, tenant_id=source_tenant_id, identity=identity),
    )


def _authorize_query_scope(
    request: Request, identity: AuthIdentity, tenant_id: str, series_ids: list[str],
) -> list[str]:
    require_tenant_read(identity, tenant_id)
    if identity.mode == "local":
        return series_ids
    services = get_services(request)
    if tenant_id == identity.public_tenant_id and not series_ids:
        offset = 0
        selected = []
        while True:
            page, total = services.library.list_public_series_page(
                tenant_id=tenant_id, media=services.media, owner_user_id=identity.user_id,
                offset=offset, limit=100,
            )
            selected.extend(item.series_id for item in page)
            offset += len(page)
            if offset >= total or not page:
                break
        if not selected:
            raise ValueError("暂无可体验的公开课程")
        return selected
    for series_id in series_ids:
        series = services.library.get_series(series_id, tenant_id=tenant_id, owner_user_id=identity.user_id)
        require_series_read(identity, series)
        if tenant_id == identity.public_tenant_id:
            services.media.require_public_series(series)
    return series_ids


async def stream_agent_messages(
    request: Request,
    conversation_id: str = Query(alias="conversationId"),
    tenant_id: str = Query(alias="tenantId"),
) -> StreamingResponse:
    service = get_services(request).agent
    identity = await run_in_threadpool(get_identity, request)
    require_tenant_read(identity, tenant_id)
    await run_in_threadpool(
        service.get_conversation,
        conversation_id,
        tenant_id=tenant_id,
        owner_user_id=identity.user_id,
    )
    return StreamingResponse(
        _agent_message_events(
            request,
            service,
            conversation_id,
            tenant_id,
            get_sse_config(request),
            media=get_services(request).media,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Accel-Buffering": "no",
        },
    )


async def _agent_message_events(
    request: Request,
    service: AgentChatService,
    conversation_id: str,
    tenant_id: str,
    config: SSEConfig,
    *, media: MediaService | None = None,
) -> AsyncIterator[str]:
    previous_fingerprint: str | None = None
    last_event_at = time.monotonic()
    while True:
        try:
            identity = await run_in_threadpool(revalidate_identity, request)
            require_tenant_read(identity, tenant_id)
            states = await run_in_threadpool(
                service.list_all_messages,
                conversation_id,
                tenant_id=tenant_id,
                owner_user_id=identity.user_id,
            )
        except (AgentConversationNotFoundError, AuthError, SeriesNotFoundError):
            return
        try:
            view = AgentMessageEventView(
                conversation_id=conversation_id,
                messages=[_agent_message_view(item, media=media, tenant_id=tenant_id, identity=identity) for item in states],
                occurred_at=now_unix_time_ms(),
            )
        except (AuthError, SeriesNotFoundError):
            return
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
    tenant_id: str | None = None, identity: AuthIdentity | None = None,
) -> AgentMessageView:
    state = AgentMessageState(**state) if isinstance(state, dict) else state
    tenant_id = state.source_tenant_id or tenant_id
    if identity is not None and identity.mode == "cloud":
        if media is None or tenant_id is None:
            raise ValueError("evidence projection requires the source Tenant and media service")
        media.require_series_access(identity, list(state.series_ids), tenant_id=tenant_id)
    result = None
    if state.search_result is not None:
        if media is None or tenant_id is None:
            raise ValueError("evidence projection requires the source Tenant and media service")
        result = _agent_search_evidence_view(
            state.search_result, media, tenant_id=tenant_id, identity=identity,
        )
    render_blocks = _agent_render_blocks(state, result)
    return AgentMessageView(
        message_id=state.message_id, conversation_id=state.conversation_id,
        series_ids=list(state.series_ids), sequence_no=state.sequence_no,
        role=state.role, status=state.status, phase=state.phase, version=state.version,
        reference=state.reference, content=state.content, model_name=state.model_name,
        search_result=result, render_blocks=render_blocks,
        timing=(
            AgentMessageTimingView.model_validate(state.timing)
            if state.timing is not None else None
        ),
        last_error=state.last_error, created_at=state.created_at,
        updated_at=state.updated_at,
    )


def _agent_render_blocks(
    state: AgentMessageState,
    result: AgentSearchEvidenceView | None,
) -> list[AgentRenderBlockView]:
    if state.role != "ASSISTANT":
        return []
    raw_blocks = (
        state.search_result.get("renderBlocks")
        if isinstance(state.search_result, dict)
        else None
    )
    if not isinstance(raw_blocks, list):
        blocks = []
        if state.content:
            blocks.append(AgentMarkdownRenderBlockView(markdown=state.content))
        return blocks

    hits_by_id = {
        hit.candidate.video_chunk_id: hit
        for hit in result.hits
    } if result is not None else {}
    blocks = []
    seen_video_ids: set[str] = set()
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "markdown":
            markdown = raw.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                blocks.append(AgentMarkdownRenderBlockView(markdown=markdown))
            continue
        if raw.get("type") == "video":
            video_chunk_id = raw.get("videoChunkId")
            if not isinstance(video_chunk_id, str) or video_chunk_id not in hits_by_id:
                _logger.warning(
                    "event=agent_invalid_video_render_block conversation_id=%s message_id=%s "
                    "video_chunk_id=%s proposed_start_seconds=%r proposed_end_seconds=%r "
                    "action=drop_unknown_chunk",
                    state.conversation_id, state.message_id, video_chunk_id,
                    raw.get("startSeconds"), raw.get("endSeconds"),
                )
                continue
            if (
                video_chunk_id not in seen_video_ids
            ):
                evidence = hits_by_id[video_chunk_id].evidence
                start_seconds = raw.get("startSeconds")
                end_seconds = raw.get("endSeconds")
                if not (
                    isinstance(start_seconds, int)
                    and not isinstance(start_seconds, bool)
                    and isinstance(end_seconds, int)
                    and not isinstance(end_seconds, bool)
                    and evidence.start_seconds <= start_seconds < end_seconds <= evidence.end_seconds
                ):
                    _logger.warning(
                        "event=agent_invalid_video_render_block conversation_id=%s message_id=%s "
                        "video_chunk_id=%s proposed_start_seconds=%r proposed_end_seconds=%r "
                        "evidence_start_seconds=%s evidence_end_seconds=%s "
                        "action=drop_invalid_range",
                        state.conversation_id, state.message_id, video_chunk_id,
                        start_seconds, end_seconds, evidence.start_seconds, evidence.end_seconds,
                    )
                    continue
                seen_video_ids.add(video_chunk_id)
                blocks.append(AgentVideoRenderBlockView(
                    video_chunk_id=video_chunk_id,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                ))
    if not any(block.type == "markdown" for block in blocks) and state.content:
        blocks.insert(0, AgentMarkdownRenderBlockView(markdown=state.content))
    return blocks


def _agent_search_evidence_view(
    result: dict,
    media: MediaService,
    *,
    tenant_id: str,
    identity: AuthIdentity | None = None,
) -> AgentSearchEvidenceView:
    if identity is not None and identity.mode == "cloud":
        media.require_series_access(
            identity, result.get("seriesIds", result.get("series_ids", [])), tenant_id=tenant_id,
        )
    evidence = search_evidence_fields(result, media, tenant_id=tenant_id)
    raw_trace = result.get("retrievalTrace", result.get("retrieval_trace", []))
    return AgentSearchEvidenceView(
        **evidence.model_dump(),
        retrieval_trace=raw_trace if isinstance(raw_trace, list) else [],
    )


_ANSWER_EVENT_MODELS = {
    "answer.started": AgentAnswerStartedView,
    "answer.evidence": AgentAnswerEvidenceView,
    "answer.block": AgentAnswerBlockView,
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
    *, media: MediaService | None = None, tenant_id: str | None = None,
    series_ids: list[str] | None = None,
) -> AsyncIterator[str]:
    last_event_at = time.monotonic()
    try:
        while not await request.is_disconnected():
            try:
                identity = await run_in_threadpool(revalidate_identity, request)
                if tenant_id is not None:
                    require_tenant_read(identity, tenant_id)
                    if media is not None and identity.mode == "cloud":
                        await run_in_threadpool(media.require_series_access, identity, series_ids or [], tenant_id=tenant_id)
            except (AuthError, AgentConversationNotFoundError, SeriesNotFoundError):
                execution.cancellation.set()
                return
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
                    data[field] = _agent_message_view(data[field], media=media, tenant_id=tenant_id, identity=identity)
            if event_type == "answer.evidence":
                if media is None or tenant_id is None:
                    raise ValueError("evidence projection requires the source Tenant and media service")
                projected = _agent_search_evidence_view(
                    data["searchResult"], media, tenant_id=tenant_id, identity=identity,
                )
                data["searchResult"] = projected
                _logger.info(
                    "agent_evidence_projected conversation_id=%s message_id=%s "
                    "hit_count=%s playable_hits=%s thumbnail_hits=%s partial=%s "
                    "warning_count=%s",
                    event.get("conversationId"), event.get("messageId"),
                    len(projected.hits),
                    sum(hit.evidence.playback_status == "READY" for hit in projected.hits),
                    sum(hit.evidence.thumbnail is not None for hit in projected.hits),
                    projected.partial, len(projected.warnings),
                )
            view = model.model_validate(data)
            last_event_at = time.monotonic()
            payload = view.model_dump_json(
                by_alias=True,
                exclude={name for name, value in view if value is None},
            )
            yield f"event: {view.type}\nid: {view.message_id}:{view.sequence}\ndata: {payload}\n\n"
    except (AuthError, SeriesNotFoundError):
        execution.cancellation.set()
        return
    finally:
        execution.detach()
