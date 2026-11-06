"""Direct Refine uses retained observations; media tools remain tested at their own boundary."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.video_parse_v2.input import FallbackModelConfig, ParseInput
from agent.video_parse_v2.refine.response import RefineModelResponse
from server.config.parse_profile import default_video_parse_v2_config
from server.service.video_parse import VideoParseRuntimeService


@pytest.mark.parametrize("vision", [None, False, True])
@pytest.mark.parametrize("voice_enabled", [False, True])
def test_refine_keeps_frozen_config_and_uses_no_media_or_voice_tools(monkeypatch, vision, voice_enabled):
    config = default_video_parse_v2_config()
    understanding = config.models.understanding.model_copy(update={
        "supports_vision_input": vision,
        "fallback": FallbackModelConfig(source="deepseek_api", model="fallback",
                                       request_timeout_seconds=90, supports_vision_input=vision),
    })
    config = config.model_copy(update={"models": config.models.model_copy(update={
        "understanding": understanding, "refine": None,
    })})
    # The production bool derives from the frozen voiceprint config.
    if voice_enabled:
        from agent.video_parse_v2.input import VoiceprintConfig
        values = config.model_dump(mode="json")
        values["voiceprint"] = VoiceprintConfig(max_tool_calls=2, execution_timeout_seconds=30).model_dump(mode="json")
        values["voiceprint_mode"] = "prefill"
        values["models"]["speaker_embedding"] = {
            "source": "speaker_cpu", "model": "campplus", "request_timeout_seconds": 30,
        }
        config = type(config).model_validate(values)
    frozen = config.model_dump(mode="json")
    result = RefineModelResponse.model_validate({"members": [{"sequence_no": 0, "persons": [], "timeline": []}]})
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = SimpleNamespace(load_task_input=Mock(side_effect=AssertionError("no media source lookup")))
    runtime._artifact_store = SimpleNamespace(resolve=Mock(side_effect=AssertionError("no new media")))
    runtime._speaker_encoder = Mock(side_effect=AssertionError("no new voice inference"))
    generated = AsyncMock(return_value=result)
    monkeypatch.setattr(runtime, "_generate_text_structured", generated)
    actual = asyncio.run(runtime.generate_interval_refinement(
        {"inputs": ParseInput(series_id="series", video_id="video", parse_config=config)},
        prompt="Complete evidence and events", remaining_seconds=12.5,
    ))
    assert actual == result
    binding, prompt = generated.await_args.args
    assert prompt == "Complete evidence and events"
    assert generated.await_args.kwargs == {"tools": (), "result_type": RefineModelResponse, "schema_name": "IntervalRefinementResult"}
    assert binding.request_timeout_seconds <= 12.5 and binding.fallback.request_timeout_seconds <= 12.5
    assert binding.max_output_tokens == understanding.max_output_tokens
    assert config.model_dump(mode="json") == frozen
    runtime._speaker_encoder.assert_not_called()
    runtime._persistence.load_task_input.assert_not_called()
    runtime._artifact_store.resolve.assert_not_called()


def test_expired_refine_deadline_does_not_call_model(monkeypatch):
    runtime = object.__new__(VideoParseRuntimeService)
    generated = AsyncMock()
    monkeypatch.setattr(runtime, "_generate_text_structured", generated)
    with pytest.raises(TimeoutError, match="deadline"):
        asyncio.run(runtime.generate_interval_refinement(
            {"inputs": SimpleNamespace(parse_config=default_video_parse_v2_config())},
            prompt="refine", remaining_seconds=0,
        ))
    generated.assert_not_awaited()
