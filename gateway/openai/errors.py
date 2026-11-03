"""Shared SDK failure classification for OpenAI-compatible model requests."""
from openai import APIConnectionError, APIError, APITimeoutError

from gateway.agent_model.errors import ModelFailureReason


def transient_failure_reason(error: APIError) -> ModelFailureReason | None:
    if isinstance(error, APITimeoutError):
        return ModelFailureReason.MODEL_TIMEOUT
    if isinstance(error, APIConnectionError):
        return ModelFailureReason.MODEL_CONNECTION_ERROR
    if getattr(error, "status_code", None) == 504:
        return ModelFailureReason.MODEL_GATEWAY_TIMEOUT
    if getattr(error, "status_code", None) in {429, 500, 502, 503}:
        return ModelFailureReason.MODEL_SERVICE_UNAVAILABLE
    return None
