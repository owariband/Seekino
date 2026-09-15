from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..repository.agent import (
    AgentConflictError,
    AgentConversationNotFoundError,
    AgentMessageInProgressError,
    AgentMessageNotFoundError,
    AgentSessionNotFoundError,
)
from ..repository.job import ParseJobNotFoundError
from ..repository.library import SeriesConflictError, SeriesNotFoundError
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
from ..service.agent import AgentModelInvocationError
from ..service.upload import UploadChecksumMismatchError
from ..service.video_retrieval import RetrievalIndexConsistencyError
from ..storage.artifact import ArtifactIntegrityError
from .schema import ApiError


def register_exception_handlers(app: FastAPI) -> None:
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
    async def value_error_handler(
        _request: Request,
        exc: ValueError,
    ) -> JSONResponse:
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
