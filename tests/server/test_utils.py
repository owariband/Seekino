from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import subprocess
import sys

import pytest
from pydantic import TypeAdapter, ValidationError

from server.model.vo.commonVO import UnixTimeMs
from server.utils import time as time_utils
from server.utils.errors import MAX_ERROR_SUMMARY_LENGTH, exception_summary
from server.utils.validation import require_nonempty_text


def test_time_helpers_use_signed_int64_unix_milliseconds(monkeypatch):
    fixed = datetime(2026, 9, 15, 12, 30, tzinfo=UTC)
    fixed_ms = 1_789_475_400_000
    monkeypatch.setattr(time_utils.time, "time_ns", lambda: fixed_ms * 1_000_000)
    assert time_utils.now_unix_time_ms() == fixed_ms
    assert time_utils.datetime_to_unix_time_ms(fixed) == fixed_ms
    assert time_utils.unix_time_ms_to_datetime(fixed_ms) == fixed
    local = datetime(2026, 9, 15, 20, 30, tzinfo=timezone(timedelta(hours=8)))
    assert time_utils.datetime_to_unix_time_ms(local) == fixed_ms
    assert time_utils.timedelta_to_ms(timedelta(seconds=1, microseconds=234_000)) == 1_234


@pytest.mark.parametrize("value", [-1, True, 1.5, "1", None, 2**63])
def test_unix_time_helper_rejects_non_int64_values(value):
    with pytest.raises(ValueError, match="signed INT64"):
        time_utils.require_unix_time_ms(value)


def test_datetime_conversion_requires_explicit_timezone():
    with pytest.raises(ValueError, match="explicit timezone"):
        time_utils.datetime_to_unix_time_ms(datetime(2026, 9, 15, 12, 30))


def test_database_datetime_helpers_normalize_to_aware_utc():
    local = datetime(2026, 9, 15, 20, 30, tzinfo=timezone(timedelta(hours=8)))
    expected = datetime(2026, 9, 15, 12, 30, tzinfo=UTC)

    assert time_utils.require_utc_datetime(local) == expected
    assert time_utils.normalize_database_utc_datetime(
        datetime(2026, 9, 15, 12, 30)
    ) == expected
    assert time_utils.normalize_database_utc_datetime(local) == expected
    assert time_utils.now_utc().tzinfo is UTC
    assert time_utils.now_utc().microsecond % 1_000 == 0


def test_utc_datetime_helper_rejects_naive_values():
    with pytest.raises(ValueError, match="timezone-aware"):
        time_utils.require_utc_datetime(datetime(2026, 9, 15, 12, 30))


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 9, 15, tzinfo=UTC), "1789411200000", 1.0, True, 2**63],
)
def test_api_unix_time_contract_accepts_only_signed_int64_milliseconds(value):
    with pytest.raises(ValidationError):
        TypeAdapter(UnixTimeMs).validate_python(value)

    assert (
        TypeAdapter(UnixTimeMs).validate_python(1_789_411_200_000)
        == 1_789_411_200_000
    )


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


def test_process_logging_writes_utf8_files_rotates_and_preserves_request_context(tmp_path):
    script = """
import logging
import sys
from pathlib import Path
from server.config.logging import LoggingConfig
from server.utils.logging import configure_logging, request_id_context

config = LoggingConfig(directory=Path(sys.argv[1]), maximum_file_size_bytes=500, backup_count=2)
configure_logging("api", environment="test", config=config)
logger = logging.getLogger("server.test")
for number in range(10):
    logger.info("rotate_%s %s", number, "x" * 180)
token = request_id_context.set("request-test")
logger.info("上传事件")
request_id_context.reset(token)
logger.info("outside_request")
logger.debug("hidden_debug")
configure_logging("mq", environment="test", config=config)
logging.getLogger("server.mq").info("claimed job=job-test")
logging.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
        capture_output=True, text=True, check=True)
    directory = tmp_path / "test"
    assert {path.name for path in directory.iterdir()} == {"api.log", "api.log.1", "api.log.2", "mq.log"}
    text = (directory / "api.log").read_text(encoding="utf-8")
    assert "INFO server.test request_id=request-test 上传事件" in text
    assert "request_id=- outside_request" in text
    assert "hidden_debug" not in text
    assert "上传事件" in result.stderr
    assert "claimed job=job-test" in (directory / "mq.log").read_text()
    assert not (tmp_path / "development").exists()
