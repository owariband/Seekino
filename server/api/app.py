from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import AsyncIterator

from fastapi import FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config import ServerSettings
from ..model.job import ParseJobStatus
from ..repository.agent import (
    AgentConflictError,
    AgentConversationNotFoundError,
    AgentMessageInProgressError,
    AgentMessageNotFoundError,
    AgentMessageState,
    AgentSessionNotFoundError,
)
from ..repository.job import ParseJobNotFoundError
from ..repository.library import (
    SeriesConflictError,
    SeriesNotFoundError,
    SeriesState,
)
from ..repository.upload import (
    UploadCompletionInProgressError,
    UploadConflictError,
    UploadSessionNotFoundError,
    UploadSeriesNotFoundError,
    UploadSegmentState,
    UploadSessionState,
    UploadStateError,
)
from ..repository.video import (
    SeriesNotReadyError,
    SeriesScopeNotFoundError,
    VideoChunkNotFoundError,
)
from ..schema import (
    AgentConversationCreateRequest,
    AgentConversationView,
    AgentMessageEventView,
    AgentMessageSendRequest,
    AgentMessageSendView,
    AgentMessageView,
    AgentSessionCreateRequest,
    AgentSessionView,
    ApiError,
    ParseJobView,
    SeriesCreateRequest,
    SeriesView,
    UploadCompleteView,
    UploadSegmentView,
    UploadSessionCreateRequest,
    UploadSessionView,
    VideoChunkCheckpointView,
    VideoChunkSearchResult,
    VideoSearchRequest,
    VideoSummary,
)
from ..service.agent import AgentConversationService, AgentModelInvocationError
from ..service.job import ParseJobDetail
from ..service.upload import UploadChecksumMismatchError
from ..service.video_retrieval import RetrievalIndexConsistencyError
from ..storage.artifact import ArtifactIntegrityError
from .dependencies import ApplicationServices, build_application_services

_CONTENT_RANGE_PATTERN = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_TERMINAL_JOB_STATUSES = {
    ParseJobStatus.SUCCEEDED.value,
    ParseJobStatus.FAILED.value,
}


def create_app(
    services: ApplicationServices | None = None,
    *,
    settings: ServerSettings | None = None,
) -> FastAPI:
    owns_services = services is None
    resolved_settings = settings
    if services is None:
        resolved_settings = resolved_settings or ServerSettings.from_environment()
        services = build_application_services(resolved_settings)
    assert services is not None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_services:
            services.close()

    app = FastAPI(
        title="AniVAG Server",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    origins = resolved_settings.allowed_origins if resolved_settings else ()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Content-Range",
                "Idempotency-Key",
                "X-Content-SHA256",
            ],
        )
    _register_exception_handlers(app)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/series/create", response_model=SeriesView, status_code=201)
    def create_series(request: Request, body: SeriesCreateRequest) -> SeriesView:
        state = _services(request).library.create_series(
            workspace_id=body.workspace_id,
            name=body.name,
            description=body.description,
            important_extra_data=body.important_extra_data,
            character_bible=body.character_bible,
        )
        return _series_view(state)

    @app.get("/api/series/list", response_model=list[SeriesView])
    def list_series(
        request: Request,
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> list[SeriesView]:
        return [
            _series_view(item)
            for item in _services(request).library.list_series(
                workspace_id=workspace_id
            )
        ]

    @app.get("/api/series/get", response_model=SeriesView)
    def get_series(
        request: Request,
        series_id: str = Query(alias="seriesId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> SeriesView:
        return _series_view(
            _services(request).library.get_series(
                series_id,
                workspace_id=workspace_id,
            )
        )

    @app.post(
        "/api/uploads/create",
        response_model=UploadSessionView,
        status_code=201,
    )
    def create_upload_session(
        body: UploadSessionCreateRequest,
        request: Request,
        series_id: str = Query(alias="seriesId"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> UploadSessionView:
        service = _services(request).uploads
        state = service.create_session(
            workspace_id=body.workspace_id,
            series_id=series_id,
            episode_order=body.episode_order,
            upload_sequence=body.upload_sequence,
            original_filename=body.original_filename,
            media_type=body.media_type,
            total_size_bytes=body.total_size_bytes,
            idempotency_key=idempotency_key,
        )
        return _upload_view(state, service.list_segments(state.upload_session_id))

    @app.get(
        "/api/uploads/get",
        response_model=UploadSessionView,
    )
    def get_upload_session(
        request: Request,
        upload_session_id: str = Query(alias="uploadSessionId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> UploadSessionView:
        service = _services(request).uploads
        state = service.get_session(upload_session_id)
        _require_workspace(state.workspace_id, workspace_id)
        return _upload_view(state, service.list_segments(upload_session_id))

    @app.put(
        "/api/uploads/segments/upload",
        response_model=UploadSegmentView,
    )
    async def upload_segment(
        request: Request,
        upload_session_id: str = Query(alias="uploadSessionId"),
        segment_number: int = Query(alias="segmentNumber"),
        content_range: str = Header(alias="Content-Range"),
        checksum_sha256: str = Header(alias="X-Content-SHA256"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> UploadSegmentView:
        service = _services(request).uploads
        current = await run_in_threadpool(service.get_session, upload_session_id)
        _require_workspace(current.workspace_id, workspace_id)
        start, end_exclusive, total = _parse_content_range(content_range)
        if total != current.total_size_bytes:
            raise ValueError("Content-Range total does not match the upload session")
        expected_start = segment_number * current.preferred_segment_size_bytes
        expected_end = min(
            expected_start + current.preferred_segment_size_bytes,
            current.total_size_bytes,
        )
        if segment_number < 0 or (start, end_exclusive) != (
            expected_start,
            expected_end,
        ):
            raise UploadConflictError(
                "Content-Range does not match this upload segment"
            )
        data = await _read_limited_body(
            request,
            maximum_bytes=end_exclusive - start,
        )
        state = await run_in_threadpool(
            service.upload_segment,
            upload_session_id,
            segment_number,
            data,
            checksum_sha256=checksum_sha256,
            start_byte=start,
            end_byte_exclusive=end_exclusive,
        )
        payload = asdict(state)
        payload.pop("artifact_uri")
        payload["upload_session_id"] = upload_session_id
        return UploadSegmentView.model_validate(payload)

    @app.post(
        "/api/uploads/complete",
        response_model=UploadCompleteView,
    )
    async def complete_upload(
        request: Request,
        upload_session_id: str = Query(alias="uploadSessionId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> UploadCompleteView:
        service = _services(request).uploads
        current = await run_in_threadpool(service.get_session, upload_session_id)
        _require_workspace(current.workspace_id, workspace_id)
        completed = await run_in_threadpool(service.complete, upload_session_id)
        segments = await run_in_threadpool(
            service.list_segments,
            upload_session_id,
        )
        return UploadCompleteView(
            upload_session=_upload_view(completed.session, segments),
            video_id=completed.video_id,
            job_id=completed.job_id,
        )

    @app.get("/api/jobs/get", response_model=ParseJobView)
    def get_job(
        request: Request,
        job_id: str = Query(alias="jobId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> ParseJobView:
        services = _services(request)
        detail = services.jobs.get(job_id)
        services.library.get_video(detail.job.video_id, workspace_id=workspace_id)
        return _job_view(detail)

    @app.get("/api/jobs/list", response_model=list[ParseJobView])
    def list_jobs(
        request: Request,
        workspace_id: str = Query("local-default", alias="workspaceId"),
        limit: int = Query(100, ge=1, le=500),
    ) -> list[ParseJobView]:
        return [
            _job_view(item)
            for item in _services(request).jobs.list(
                workspace_id=workspace_id,
                limit=limit,
            )
        ]

    @app.get("/api/jobs/events")
    async def stream_job_events(
        request: Request,
        job_id: str = Query(alias="jobId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> StreamingResponse:
        services = _services(request)
        initial = await run_in_threadpool(services.jobs.get, job_id)
        await run_in_threadpool(
            services.library.get_video,
            initial.job.video_id,
            workspace_id=workspace_id,
        )
        return StreamingResponse(
            _job_events(request, services, job_id, initial),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/videos/content")
    def get_video_content(
        request: Request,
        video_id: str = Query(alias="videoId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> FileResponse:
        media = _services(request).media.get_original(
            video_id,
            workspace_id=workspace_id,
        )
        return FileResponse(
            media.path,
            media_type=media.media_type,
            filename=media.path.name,
            content_disposition_type="inline",
        )

    @app.post("/api/search", response_model=VideoChunkSearchResult)
    def search_video_chunks(
        body: VideoSearchRequest,
        request: Request,
    ) -> VideoChunkSearchResult:
        services = _services(request)
        for series_id in dict.fromkeys(body.series_ids):
            services.library.get_series(
                series_id,
                workspace_id=body.workspace_id,
            )
        return services.retrieval.search(
            body.query,
            series_ids=body.series_ids,
            top_k=body.top_k,
        )

    @app.post(
        "/api/agent/sessions/create",
        response_model=AgentSessionView,
        status_code=201,
    )
    def create_agent_session(
        request: Request,
        body: AgentSessionCreateRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> AgentSessionView:
        state = _services(request).agent.create_session(
            workspace_id=body.workspace_id,
            name=body.name,
            idempotency_key=idempotency_key,
        )
        return AgentSessionView.model_validate(asdict(state))

    @app.get(
        "/api/agent/sessions/list",
        response_model=list[AgentSessionView],
    )
    def list_agent_sessions(
        request: Request,
        workspace_id: str = Query("local-default", alias="workspaceId"),
        limit: int = Query(100, ge=1, le=500),
    ) -> list[AgentSessionView]:
        return [
            AgentSessionView.model_validate(asdict(item))
            for item in _services(request).agent.list_sessions(
                workspace_id=workspace_id,
                limit=limit,
            )
        ]

    @app.get("/api/agent/sessions/get", response_model=AgentSessionView)
    def get_agent_session(
        request: Request,
        session_id: str = Query(alias="sessionId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> AgentSessionView:
        state = _services(request).agent.get_session(
            session_id,
            workspace_id=workspace_id,
        )
        return AgentSessionView.model_validate(asdict(state))

    @app.post(
        "/api/agent/conversations/create",
        response_model=AgentConversationView,
        status_code=201,
    )
    def create_agent_conversation(
        request: Request,
        body: AgentConversationCreateRequest,
        session_id: str = Query(alias="sessionId"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> AgentConversationView:
        state = _services(request).agent.create_conversation(
            session_id,
            workspace_id=body.workspace_id,
            title=body.title,
            series_ids=body.series_ids,
            idempotency_key=idempotency_key,
        )
        return AgentConversationView.model_validate(asdict(state))

    @app.get(
        "/api/agent/conversations/list",
        response_model=list[AgentConversationView],
    )
    def list_agent_conversations(
        request: Request,
        session_id: str = Query(alias="sessionId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
        limit: int = Query(100, ge=1, le=500),
    ) -> list[AgentConversationView]:
        return [
            AgentConversationView.model_validate(asdict(item))
            for item in _services(request).agent.list_conversations(
                session_id,
                workspace_id=workspace_id,
                limit=limit,
            )
        ]

    @app.get(
        "/api/agent/conversations/get",
        response_model=AgentConversationView,
    )
    def get_agent_conversation(
        request: Request,
        conversation_id: str = Query(alias="conversationId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> AgentConversationView:
        state = _services(request).agent.get_conversation(
            conversation_id,
            workspace_id=workspace_id,
        )
        return AgentConversationView.model_validate(asdict(state))

    @app.get(
        "/api/agent/messages/list",
        response_model=list[AgentMessageView],
    )
    def list_agent_messages(
        request: Request,
        conversation_id: str = Query(alias="conversationId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
        after_sequence_no: int = Query(-1, ge=-1, alias="afterSequenceNo"),
        limit: int = Query(200, ge=1, le=500),
    ) -> list[AgentMessageView]:
        return [
            _agent_message_view(item)
            for item in _services(request).agent.list_messages(
                conversation_id,
                workspace_id=workspace_id,
                after_sequence_no=after_sequence_no,
                limit=limit,
            )
        ]

    @app.post(
        "/api/agent/messages/send",
        response_model=AgentMessageSendView,
    )
    def send_agent_message(
        request: Request,
        body: AgentMessageSendRequest,
        conversation_id: str = Query(alias="conversationId"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> AgentMessageSendView:
        result = _services(request).agent.send_message(
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

    @app.get("/api/agent/messages/events")
    async def stream_agent_messages(
        request: Request,
        conversation_id: str = Query(alias="conversationId"),
        workspace_id: str = Query("local-default", alias="workspaceId"),
    ) -> StreamingResponse:
        service = _services(request).agent
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
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )


async def _job_events(
    request: Request,
    services: ApplicationServices,
    job_id: str,
    initial: ParseJobDetail,
) -> AsyncIterator[str]:
    previous_fingerprint: str | None = None
    detail = initial
    idle_polls = 0
    while True:
        view = _job_view(detail)
        payload = view.model_dump_json(by_alias=True)
        fingerprint = _job_event_fingerprint(view)
        if fingerprint != previous_fingerprint:
            yield f"event: job\ndata: {payload}\n\n"
            previous_fingerprint = fingerprint
            idle_polls = 0
        else:
            idle_polls += 1
            if idle_polls >= 15:
                yield ": keep-alive\n\n"
                idle_polls = 0
        if detail.job.status in _TERMINAL_JOB_STATUSES:
            return
        if await request.is_disconnected():
            return
        await asyncio.sleep(1)
        detail = await run_in_threadpool(services.jobs.get, job_id)


async def _agent_message_events(
    request: Request,
    service: AgentConversationService,
    conversation_id: str,
    workspace_id: str,
) -> AsyncIterator[str]:
    previous_fingerprint: str | None = None
    idle_polls = 0
    while True:
        states = await run_in_threadpool(
            service.list_messages,
            conversation_id,
            workspace_id=workspace_id,
            after_sequence_no=-1,
            limit=500,
        )
        view = AgentMessageEventView(
            conversation_id=conversation_id,
            messages=[_agent_message_view(item) for item in states],
        )
        payload = view.model_dump_json(by_alias=True)
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if fingerprint != previous_fingerprint:
            yield f"event: messages\ndata: {payload}\n\n"
            previous_fingerprint = fingerprint
            idle_polls = 0
        else:
            idle_polls += 1
            if idle_polls >= 15:
                yield ": keep-alive\n\n"
                idle_polls = 0
        if await request.is_disconnected():
            return
        await asyncio.sleep(1)


def _job_event_fingerprint(view: ParseJobView) -> str:
    """Ignore lease heartbeats when deciding whether the UI state changed."""

    semantic_state = view.model_dump_json(
        by_alias=True,
        exclude={"lease_expires_at", "updated_at"},
    )
    return hashlib.sha256(semantic_state.encode("utf-8")).hexdigest()


def _services(request: Request) -> ApplicationServices:
    return request.app.state.services


def _series_view(state: SeriesState) -> SeriesView:
    payload = asdict(state)
    payload["videos"] = [
        VideoSummary.model_validate(item) for item in payload["videos"]
    ]
    return SeriesView.model_validate(payload)


def _upload_view(
    state: UploadSessionState,
    segments: list[UploadSegmentState],
) -> UploadSessionView:
    payload = asdict(state)
    present = {item.segment_number for item in segments}
    payload["missing_segment_numbers"] = [
        number for number in range(payload["total_segments"]) if number not in present
    ]
    return UploadSessionView.model_validate(payload)


def _job_view(detail: ParseJobDetail) -> ParseJobView:
    payload = asdict(detail.job)
    payload.pop("lease_owner")
    payload.pop("lease_version")
    payload.pop("heartbeat_at")
    payload["checkpoints"] = [
        VideoChunkCheckpointView.model_validate(asdict(item))
        for item in detail.checkpoints
    ]
    return ParseJobView.model_validate(payload)


def _agent_message_view(state: AgentMessageState) -> AgentMessageView:
    return AgentMessageView.model_validate(asdict(state))


def _parse_content_range(value: str) -> tuple[int, int, int]:
    match = _CONTENT_RANGE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("Content-Range must use 'bytes START-END/TOTAL'")
    start, inclusive_end, total = (int(item) for item in match.groups())
    if inclusive_end < start or total <= inclusive_end:
        raise ValueError("Content-Range contains an invalid byte range")
    return start, inclusive_end + 1, total


async def _read_limited_body(request: Request, *, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum_bytes:
            raise ValueError("upload segment body exceeds its Content-Range")
        chunks.append(chunk)
    if size != maximum_bytes:
        raise ValueError("upload segment body size does not match Content-Range")
    return b"".join(chunks)


def _require_workspace(actual: str, requested: str) -> None:
    if actual != requested:
        raise UploadSessionNotFoundError("resource does not exist in this workspace")


def _register_exception_handlers(app: FastAPI) -> None:
    not_found = (
        AgentConversationNotFoundError,
        AgentMessageNotFoundError,
        AgentSessionNotFoundError,
        ParseJobNotFoundError,
        SeriesNotFoundError,
        SeriesScopeNotFoundError,
        UploadSessionNotFoundError,
        UploadSeriesNotFoundError,
        VideoChunkNotFoundError,
        FileNotFoundError,
    )
    conflict = (
        AgentConflictError,
        AgentMessageInProgressError,
        ArtifactIntegrityError,
        RetrievalIndexConsistencyError,
        SeriesConflictError,
        SeriesNotReadyError,
        UploadChecksumMismatchError,
        UploadCompletionInProgressError,
        UploadConflictError,
        UploadStateError,
    )

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        return _error_response(400, "INVALID_REQUEST", str(exc))

    for exception_type in not_found:
        app.add_exception_handler(
            exception_type,
            lambda _request, exc, code="NOT_FOUND": _error_response(
                404,
                code,
                str(exc),
            ),
        )
    for exception_type in conflict:
        app.add_exception_handler(
            exception_type,
            lambda _request, exc, code="CONFLICT": _error_response(
                409,
                code,
                str(exc),
            ),
        )

    @app.exception_handler(AgentModelInvocationError)
    async def agent_model_error_handler(
        _request: Request,
        exc: AgentModelInvocationError,
    ) -> JSONResponse:
        return _error_response(502, "AGENT_MODEL_ERROR", str(exc))


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    error = ApiError(code=code, message=message or code)
    return JSONResponse(
        status_code=status_code,
        content=error.model_dump(mode="json", by_alias=True),
    )
