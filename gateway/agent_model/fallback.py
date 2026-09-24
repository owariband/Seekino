from __future__ import annotations

import logging


def log_model_fallback(
    logger: logging.Logger,
    *,
    primary_provider: str,
    primary_model: str,
    fallback_provider: str,
    fallback_model: str,
    error: BaseException,
) -> None:
    """Log fallback routing without provider payloads or exception messages."""

    logger.warning(
        "event=agent_model_fallback primary_provider=%s primary_model=%s "
        "fallback_provider=%s fallback_model=%s error_type=%s",
        primary_provider,
        primary_model,
        fallback_provider,
        fallback_model,
        type(error).__name__,
    )


__all__ = ["log_model_fallback"]
