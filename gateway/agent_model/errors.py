from __future__ import annotations

from enum import StrEnum


class ModelInputTooLargeError(RuntimeError):
    """The complete model message state exceeded an application context budget."""


class ModelFailureReason(StrEnum):
    """Provider-level reasons that callers can map without parsing messages."""

    OUTPUT_TOKEN_LIMIT = "output_token_limit"
    REQUIRED_TOOL_MISSING = "required_tool_missing"
    LOOP_LIMIT = "loop_limit"
    INVALID_MODEL_RESPONSE = "invalid_model_response"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_CONNECTION_ERROR = "model_connection_error"
    MODEL_GATEWAY_TIMEOUT = "model_gateway_timeout"
    MODEL_SERVICE_UNAVAILABLE = "model_service_unavailable"


class ModelFailureError(RuntimeError):
    """A model failure with a stable reason for higher-level routing."""

    default_reason: ModelFailureReason = ModelFailureReason.INVALID_MODEL_RESPONSE

    def __init__(
        self,
        message: str,
        *,
        reason: ModelFailureReason | None = None,
    ) -> None:
        self.reason: ModelFailureReason = reason or self.default_reason
        super().__init__(message)


class QueryModelIncompleteError(ModelFailureError):
    """A structured completion returned an invalid or non-terminal response."""


class MissingStructuredQueryToolCallError(ModelFailureError):
    """A structured completion stopped without any required tool call."""

    default_reason = ModelFailureReason.REQUIRED_TOOL_MISSING


class MissingStructuredQueryDraftError(ModelFailureError):
    """The structured loop ended without its required result."""

    default_reason = ModelFailureReason.REQUIRED_TOOL_MISSING


class MissingRequiredFunctionCallError(ModelFailureError):
    """A model stopped before making a required function call."""

    default_reason = ModelFailureReason.REQUIRED_TOOL_MISSING


class AgentModelTimeoutError(TimeoutError):
    """An Agent model stage exhausted its caller-provided time budget."""

    def __init__(self, message: str) -> None:
        self.reason: ModelFailureReason = ModelFailureReason.MODEL_TIMEOUT
        super().__init__(message)


class OpenAIFunctionCallingError(ModelFailureError):
    """An OpenAI-compatible adapter failed or returned an invalid response."""


class ArkFunctionCallingError(ModelFailureError):
    """Ark failed or returned an invalid agent-loop response."""


__all__ = [
    "AgentModelTimeoutError",
    "ArkFunctionCallingError",
    "MissingRequiredFunctionCallError",
    "MissingStructuredQueryDraftError",
    "MissingStructuredQueryToolCallError",
    "ModelFailureError",
    "ModelFailureReason",
    "ModelInputTooLargeError",
    "OpenAIFunctionCallingError",
    "QueryModelIncompleteError",
]
