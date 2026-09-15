from __future__ import annotations

MAX_ERROR_SUMMARY_LENGTH = 8192


def exception_summary(exc: Exception) -> str:
    message = str(exc).strip()
    summary = type(exc).__name__ if not message else f"{type(exc).__name__}: {message}"
    return summary[:MAX_ERROR_SUMMARY_LENGTH]
