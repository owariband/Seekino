from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from server.utils import time as time_utils
from server.utils.errors import MAX_ERROR_SUMMARY_LENGTH, exception_summary
from server.utils.validation import require_nonempty_text


def test_time_helpers_keep_aware_and_naive_utc_semantics(monkeypatch):
    assert time_utils.utc_now().tzinfo is UTC
    fixed = datetime(2026, 9, 15, 12, 30, tzinfo=UTC)
    monkeypatch.setattr(time_utils, "utc_now", lambda: fixed)

    assert time_utils.utc_now_naive() == datetime(2026, 9, 15, 12, 30)
    assert time_utils.utc_now_naive().tzinfo is None
    local = datetime(2026, 9, 15, 20, 30, tzinfo=timezone(timedelta(hours=8)))
    assert time_utils.as_naive_utc(local) == time_utils.utc_now_naive()
    naive = datetime(2026, 9, 15, 12, 30)
    assert time_utils.as_naive_utc(naive) is naive


@pytest.mark.parametrize("value", [None, 42, "", " spaced", "spaced ", "\n"])
def test_text_validation_rejects_invalid_input_without_silent_trim(value):
    with pytest.raises(ValueError, match="name must be a non-empty string"):
        require_nonempty_text(value, "name")


def test_text_validation_preserves_maximum_boundary():
    assert require_nonempty_text("名字", "name", maximum=2) == "名字"
    with pytest.raises(ValueError, match="at most 2 characters"):
        require_nonempty_text("名字长", "name", maximum=2)


def test_exception_summary_preserves_type_message_and_storage_limit():
    assert exception_summary(ValueError()) == "ValueError"
    assert exception_summary(ValueError(" problem ")) == "ValueError: problem"
    summary = exception_summary(RuntimeError("x" * MAX_ERROR_SUMMARY_LENGTH))
    assert summary.startswith("RuntimeError: ")
    assert len(summary) == MAX_ERROR_SUMMARY_LENGTH
