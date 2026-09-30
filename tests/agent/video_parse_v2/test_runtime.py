from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.video_parse_v2 import ParseAgent
from agent.video_parse_v2.errors import ParseError
from agent.video_parse_v2.runtime import LeaseToken, VideoRunSnapshot, VideoTaskStatus

from tests.agent.video_parse_v2.test_framework import _inputs


def test_lease_token_is_an_immutable_exact_attempt_fence() -> None:
    token = LeaseToken(
        owner="worker-1",
        version=4,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    with pytest.raises(FrozenInstanceError):
        token.version = 5  # type: ignore[misc]
    with pytest.raises(ValueError, match="positive integer"):
        LeaseToken(owner="worker-1", version=0, expires_at=token.expires_at)
    with pytest.raises(ValueError, match="timezone-aware"):
        LeaseToken(owner="worker-1", version=1, expires_at=datetime.now())


def test_active_video_run_rejects_missing_lease_before_any_write() -> None:
    runtime = SimpleNamespace(
        load_video_run=AsyncMock(return_value=VideoRunSnapshot(
            "video-task-1",
            VideoTaskStatus.PREPARING,
            _inputs(),
            20,
            None,
        )),
        ensure_video_chunks=AsyncMock(),
    )
    with pytest.raises(ParseError, match="missing its lease fence"):
        asyncio.run(ParseAgent(runtime=runtime).run("video-task-1"))
    runtime.ensure_video_chunks.assert_not_awaited()


def test_complete_video_run_needs_no_lease_or_external_replay() -> None:
    runtime = SimpleNamespace(load_video_run=AsyncMock(return_value=VideoRunSnapshot(
        "video-task-1",
        VideoTaskStatus.COMPLETE,
        _inputs(),
        20,
        None,
    )))
    result = asyncio.run(ParseAgent(runtime=runtime).run("video-task-1"))
    assert result["status"] == VideoTaskStatus.COMPLETE
