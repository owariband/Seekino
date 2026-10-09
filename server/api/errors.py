from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from agent.chat.common.errors import AgentContextBudgetError, AgentModelInvocationError

from ..config.global_agent_settings import GlobalAgentSettingsConflictError
from ..model.vo.commonVO import ApiError, FieldError
from ..repository.agent import (
    AgentConflictError,
    AgentConversationNotFoundError,
    AgentMessageInProgressError,
    AgentMessageNotFoundError,
    AgentReferenceInvalidError,
)
from ..repository.job import ParseJobNotFoundError
from ..repository.library import (
    SeriesConflictError,
    SeriesNotFoundError,
    VideoEpisodeOrderConflictError,
)
from ..repository.upload import (
    UploadCompletionInProgressError,
    UploadConflictError,
    UploadSessionNotFoundError,
    UploadSeriesNotFoundError,
    UploadStateError,
)
from ..repository.video import (
    SeriesNotReadyError,
    SeriesScopeNotFoundError,
    VideoChunkNotFoundError,
)
from ..service.media import MediaMissingError, MediaNotReadyError
from ..service.upload import UploadChecksumMismatchError
from ..service.video_retrieval import RetrievalIndexConsistencyError
from ..storage.artifact import ArtifactIntegrityError

_logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    not_found = (
        AgentConversationNotFoundError,
        AgentMessageNotFoundError,
        ParseJobNotFoundError,
        SeriesNotFoundError,
        SeriesScopeNotFoundError,
        UploadSessionNotFoundError,
        UploadSeriesNotFoundError,
        VideoChunkNotFoundError,
        FileNotFoundError,
    )
    conflict = (
        GlobalAgentSettingsConflictError,
        AgentConflictError,
        AgentMessageInProgressError,
        ArtifactIntegrityError,
        RetrievalIndexConsistencyError,
        SeriesConflictError,
        VideoEpisodeOrderConflictError,
        SeriesNotReadyError,
        UploadCompletionInProgressError,
        UploadConflictError,
        UploadStateError,
    )

    @app.exception_handler(ValueError)
    async def value_error_handler(
        request: Request,
        exc: ValueError,
    ) -> JSONResponse:
        return _error_response(request, 400, "INVALID_REQUEST", str(exc))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [FieldError(field=".".join(str(part) for part in error["loc"] if part not in {"body", "query", "header"}), message=error["msg"]) for error in exc.errors()]
        return _error_response(request, 422, "VALIDATION_ERROR", "请检查请求参数", field_errors=fields)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        return _error_response(request, exc.status_code, "NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST", str(exc.detail), headers=exc.headers)

    @app.exception_handler(PermissionError)
    async def permission_error_handler(request: Request, exc: PermissionError) -> JSONResponse:
        return _error_response(request, 403, "PERMISSION_DENIED", "无法读取或写入媒体文件，请检查目录权限")

    for exception_type in not_found:
        app.add_exception_handler(
            exception_type,
            lambda request, exc, code="NOT_FOUND": _error_response(
                request,
                404,
                code,
                "请求的资源不存在或无法访问",
            ),
        )
    for exception_type in conflict:
        app.add_exception_handler(
            exception_type,
            lambda request, exc, code="CONFLICT": _error_response(
                request,
                409,
                code,
                "请求与当前资源状态冲突，请刷新状态后重试",
            ),
        )

    @app.exception_handler(AgentContextBudgetError)
    async def agent_context_budget_error_handler(
        request: Request,
        exc: AgentContextBudgetError,
    ) -> JSONResponse:
        return _error_response(
            request, 422, "AGENT_CONTEXT_BUDGET_EXCEEDED",
            "本次上下文较多，请缩小系列范围、减少候选、精简系列背景或新建对话后重试",
        )

    @app.exception_handler(AgentModelInvocationError)
    async def agent_model_error_handler(
        request: Request,
        exc: AgentModelInvocationError,
    ) -> JSONResponse:
        return _error_response(request, 502, "AGENT_MODEL_ERROR", "Agent 服务暂时无法完成回答，请稍后重试")

    for exception_type, status, code, message in (
        (AgentReferenceInvalidError, 400, "REFERENCE_INVALID", "所引用的候选片段无效，请重新选择"),
        (AgentMessageInProgressError, 409, "MESSAGE_IN_PROGRESS", "当前对话的回答仍在生成，请等待完成"),
        (SeriesNotReadyError, 409, "SERIES_NOT_READY", "所选系列尚未完成建库，请稍后重试"),
        (UploadChecksumMismatchError, 422, "CHECKSUM_MISMATCH", "文件校验失败，请检查并重新上传"),
        (MediaMissingError, 410, "MEDIA_MISSING", "原媒体文件已丢失"),
        (MediaNotReadyError, 409, "MEDIA_NOT_READY", "媒体正在准备或尚无兼容格式"),
    ):
        app.add_exception_handler(exception_type, lambda request, exc, status=status, code=code, message=message: _error_response(request, status, code, message))

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        _logger.exception("request_failed exception_type=%s", type(exc).__name__)
        return _error_response(request, 500, "INTERNAL_ERROR", "服务处理失败，请保留请求编号并重试")


def _error_response(request: Request, status_code: int, code: str, message: str, *, field_errors: list[FieldError] | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or str(uuid4())
    error = ApiError(code=code, message=message or code, request_id=request_id, field_errors=field_errors or [])
    return JSONResponse(
        status_code=status_code,
        content=error.model_dump(mode="json", by_alias=True),
        headers={**(headers or {}), "X-Request-Id": request_id},
    )
