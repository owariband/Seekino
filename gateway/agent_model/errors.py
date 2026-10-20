from __future__ import annotations

from enum import StrEnum

from openai_codex.errors import ServerBusyError


class ModelInputTooLargeError(RuntimeError):
    """The complete model message state exceeded an application context budget."""


class ModelFailureReason(StrEnum):
    """Provider-level reasons that callers can map without parsing messages."""

    OUTPUT_TOKEN_LIMIT = "output_token_limit"
    REQUIRED_TOOL_MISSING = "required_tool_missing"
    LOOP_LIMIT = "loop_limit"
    INVALID_MODEL_RESPONSE = "invalid_model_response"
    MODEL_TIMEOUT = "model_timeout"
    PROVIDER_CAPACITY = "provider_capacity"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"


def provider_failure_reason(error: BaseException) -> ModelFailureReason | None:
    """Prefer provider codes and status over the unstructured error message."""
    codes: list[object] = []
    statuses: list[object] = []
    structured = False

    def collect(value: object, *, code: bool = False) -> None:
        nonlocal structured
        if value is None:
            return
        if code:
            structured = True
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            value = dump(mode="json", by_alias=True)
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"code", "type", "codexErrorInfo", "codex_error_info", "errorInfo"}:
                    collect(item, code=True)
                elif key in {"status_code", "httpStatusCode", "http_status_code"}:
                    statuses.append(item)
                elif isinstance(item, (dict, list)):
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item, code=code)
        elif code:
            codes.append(value)

    for name in ("provider_code", "code", "type", "codex_error_info"):
        collect(getattr(error, name, None), code=True)
    collect(getattr(error, "data", None), code=True)
    collect(getattr(error, "body", None))
    status = getattr(error, "status_code", None)
    if status is not None:
        statuses.append(status)
    http_statuses = [status for status in statuses if isinstance(status, int) and not isinstance(status, bool)]
    normalized = {
        str(code).replace("_", "").replace("-", "").lower()
        for code in codes
    }
    if any(status in {401, 402, 403} for status in http_statuses) or normalized.intersection({
        "insufficientquota", "quotaexceeded", "usagelimitexceeded", "usagelimitreached",
        "accountoverdueerror", "billinghardlimitreached", "billingnotactive",
        "insufficientbalance", "unauthorized", "authenticationerror", "permissiondenied",
        "badrequest", "invalidrequesterror", "contentfilter", "contextwindowexceeded",
        "sessionbudgetexceeded",
    }):
        return None
    if normalized.intersection({
        "ratelimitexceeded", "ratelimiterror", "ratelimitreached", "toomanyrequests",
    }):
        return ModelFailureReason.PROVIDER_RATE_LIMIT
    if normalized.intersection({
        "serveroverloaded", "modeloverloaded", "modelatcapacity", "capacityexceeded",
        "modelcapacityexceeded", "overloadederror",
    }):
        return ModelFailureReason.PROVIDER_CAPACITY
    if any(isinstance(code, str) for code in codes):
        return None
    if isinstance(error, ServerBusyError):
        return ModelFailureReason.PROVIDER_CAPACITY
    if 429 in http_statuses:
        return ModelFailureReason.PROVIDER_RATE_LIMIT
    if 503 in http_statuses:
        return ModelFailureReason.PROVIDER_CAPACITY
    if structured or statuses:
        return None
    message = str(error).strip().lower()
    if message in {
        "selected model is at capacity. please try a different model.",
        "selected model is at capacity.",
    }:
        return ModelFailureReason.PROVIDER_CAPACITY
    if message in {"rate limit exceeded", "rate limit exceeded.", "too many requests"}:
        return ModelFailureReason.PROVIDER_RATE_LIMIT
    return None


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
    "provider_failure_reason",
]
