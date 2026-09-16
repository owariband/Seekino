from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import subprocess
import sys

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
configure_logging("worker", environment="test", config=config)
logging.getLogger("server.worker").info("claimed job=job-test")
logging.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
        capture_output=True, text=True, check=True)
    directory = tmp_path / "test"
    assert {path.name for path in directory.iterdir()} == {"api.log", "api.log.1", "api.log.2", "worker.log"}
    text = (directory / "api.log").read_text(encoding="utf-8")
    assert "INFO server.test request_id=request-test 上传事件" in text
    assert "request_id=- outside_request" in text
    assert "hidden_debug" not in text
    assert "上传事件" in result.stderr
    assert "claimed job=job-test" in (directory / "worker.log").read_text()
    assert not (tmp_path / "development").exists()
