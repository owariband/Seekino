import logging

import pytest

from gateway.constants import (
    ARK_PROVIDER,
    DEEPSEEK_NON_THINKING_EXTRA_BODY,
    DEEPSEEK_PROVIDER,
)
from gateway.model_utils import (
    deepseek_non_thinking_extra_body,
    log_model_fallback,
)


def test_provider_names_are_stable() -> None:
    assert ARK_PROVIDER == "ark"
    assert DEEPSEEK_PROVIDER == "deepseek"


def test_deepseek_non_thinking_extra_body_returns_independent_dicts() -> None:
    first = deepseek_non_thinking_extra_body()
    second = deepseek_non_thinking_extra_body()

    assert first == second == {"thinking": {"type": "disabled"}}
    assert first is not second
    assert first["thinking"] is not second["thinking"]

    first["thinking"]["type"] = "enabled"

    assert deepseek_non_thinking_extra_body() == {
        "thinking": {"type": "disabled"},
    }


def test_deepseek_non_thinking_constant_is_read_only() -> None:
    with pytest.raises(TypeError):
        DEEPSEEK_NON_THINKING_EXTRA_BODY["thinking"]["type"] = "enabled"  # type: ignore[index]


def test_log_model_fallback_excludes_exception_message(caplog) -> None:
    logger = logging.getLogger("tests.gateway.model_fallback")
    sensitive_detail = "prompt-or-api-key-must-not-appear"
    error = RuntimeError(sensitive_detail)
    caplog.set_level(logging.WARNING, logger=logger.name)

    log_model_fallback(
        logger,
        primary_provider=DEEPSEEK_PROVIDER,
        primary_model="deepseek-flash",
        fallback_provider=ARK_PROVIDER,
        fallback_model="doubao-lite",
        error=error,
    )

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert message == (
        "event=agent_model_fallback primary_provider=deepseek "
        "primary_model=deepseek-flash fallback_provider=ark "
        "fallback_model=doubao-lite error_type=RuntimeError"
    )
    assert sensitive_detail not in message
