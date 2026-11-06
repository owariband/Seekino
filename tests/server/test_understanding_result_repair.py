from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.video_parse_v2.input import FallbackModelConfig
from agent.video_parse_v2.understanding.response import UnderstandingModelResponse
from server.config.parse_profile import default_video_parse_v2_config
from server.service.video_parse import VideoParseRuntimeService


def test_understanding_repair_uses_text_and_frozen_model_with_remaining_budget():
    config = default_video_parse_v2_config()
    binding = config.models.understanding.model_copy(update={
        "fallback": FallbackModelConfig(source="deepseek_api", model="fallback", request_timeout_seconds=90),
    })
    config = config.model_copy(update={"models": config.models.model_copy(update={"understanding": binding})})
    frozen = config.model_dump(mode="json")
    execution = SimpleNamespace(inputs=SimpleNamespace(parse_config=config))
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._ensure_chunk_media = AsyncMock(side_effect=AssertionError("no repeated media"))
    result = UnderstandingModelResponse(root={"persons": [], "timeline": []})
    runtime._generate_text_structured = AsyncMock(return_value=result)
    assert asyncio.run(runtime.repair_chunk_understanding(execution, prompt="original JSON and field paths", remaining_seconds=12.5)) == result
    used, prompt = runtime._generate_text_structured.await_args.args
    assert used.model == binding.model and used.source == binding.source
    assert used.request_timeout_seconds <= 12.5 and used.fallback.request_timeout_seconds <= 12.5
    assert prompt == "original JSON and field paths"
    assert runtime._generate_text_structured.await_args.kwargs == {
        "tools": (), "result_type": UnderstandingModelResponse, "schema_name": "video_parse_v2_chunk_repair",
    }
    assert config.model_dump(mode="json") == frozen
    runtime._ensure_chunk_media.assert_not_awaited()


def test_expired_understanding_repair_never_calls_model():
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._generate_text_structured = AsyncMock()
    with pytest.raises(TimeoutError, match="deadline"):
        asyncio.run(runtime.repair_chunk_understanding(None, prompt="invalid JSON", remaining_seconds=0))
    runtime._generate_text_structured.assert_not_awaited()
