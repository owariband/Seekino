from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent.utils.context_canvas import ContextCanvas
from agent.utils.errors import AgentToolFeedbackError
from agent.video_parse_v2 import ParseAgent
from agent.video_parse_v2.input import ParseInput
from agent.video_parse_v2.scheduler import ChunkScheduler
from agent.video_parse_v2.state import (
    ChunkScheduleEntry, ChunkScheduleSnapshot, ChunkTaskStatus, VideoRunSnapshot, VideoTaskStatus,
)
from agent.video_parse_v2.voiceprint import (
    CompareSpeakerArguments, RegisterVoiceprintArguments, VoiceprintAgent,
    build_compare_speaker_tool, build_tools,
)
from engine.parsev2.voiceprint import Voiceprint, VoiceprintReference
from tests.agent.video_parse_v2.test_framework import _inputs, _lease, _result


def _voice_inputs() -> ParseInput:
    values = _inputs().model_dump(mode="json")
    config = values["parse_config"]
    config["voiceprint"] = {"max_tool_calls": 20, "execution_timeout_seconds": 120.0}
    config["models"]["voiceprint"] = dict(config["models"]["understanding"])
    config["models"]["speaker_embedding"] = {
        "source": "local", "model": "campplus", "request_timeout_seconds": 60.0,
    }
    return ParseInput.model_validate(values)


def _snapshot():
    return {
        "video_parse_task_id": "task", "inputs": _voice_inputs(),
        "member_sequence_nos": {"chunk": 0}, "member_versions": {"chunk": 1},
        "member_contents": {"chunk": _result()},
        "sources": {"issued-source": {"chunk_id": "chunk", "start_ms": 1000, "end_ms": 2000}},
        "member_evidence": {"chunk": {"asr": {"transcript": []}}},
    }


def _reference_values():
    return {
        "chunk_id": "chunk", "start_ms": 1000, "end_ms": 2000,
        "vector": [1.0] + [0.0] * 191, "source_version": 1,
        "event_fingerprint": "a" * 64, "pcm_sha256": "b" * 64,
        "duration_ms": 1000, "selection_basis": "清楚的独立单人发言，有人物身份依据",
    }


def test_voiceprint_contract_round_trips_without_redundant_parent_identity():
    voice = Voiceprint.model_validate({
        "ready": True, "voiceprintList": [{
            "speaker": "爽世", "model": {"name": "campplus", "encoder_revision": "sha256:weights"},
            "references": [_reference_values()], "confidence": None,
        }],
    })
    assert Voiceprint.model_validate_json(voice.model_dump_json()) == voice
    assert set(voice.model_dump()) == {"ready", "voiceprintList"}
    assert Voiceprint().ready is False
    assert Voiceprint().voiceprintList == []


@pytest.mark.parametrize("changes", [
    {"start_ms": 2000}, {"duration_ms": 999}, {"source_version": 0},
    {"vector": [0.0] * 192}, {"vector": [1.0] * 191},
    {"vector": [float("nan")] + [0.0] * 191},
    {"vector": [float("inf")] + [0.0] * 191}, {"pcm_sha256": "invented"},
    {"local_path": "/private/source.wav"},
])
def test_voiceprint_contract_rejects_invalid_references(changes):
    with pytest.raises(ValidationError):
        VoiceprintReference.model_validate({**_reference_values(), **changes})


def test_optional_config_preserves_historical_behavior_and_requires_both_models():
    assert _inputs().parse_config.voiceprint is None
    enabled = _voice_inputs()
    assert ParseInput.model_validate_json(enabled.model_dump_json()) == enabled
    for model in ("voiceprint", "speaker_embedding"):
        values = enabled.model_dump(mode="json")
        values["parse_config"]["models"][model] = None
        with pytest.raises(ValidationError, match="required when voiceprint is enabled"):
            ParseInput.model_validate(values)


def test_voiceprint_fields_do_not_change_historical_request_fingerprints():
    historical = _inputs().model_dump(mode="json")
    config = historical["parse_config"]
    config.pop("voiceprint", None)
    for name in ("voiceprint", "speaker_embedding"):
        config["models"].pop(name, None)
    # Existing optional fields retain their historical representation.
    assert config["prefix_context"] is None
    assert config["models"]["refine"] is None
    restored = ParseInput.model_validate(historical)
    assert restored.model_dump(mode="json") == historical
    assert json.loads(restored.model_dump_json()) == historical

    enabled = _voice_inputs().model_dump(mode="json")["parse_config"]
    assert enabled["voiceprint"] == {"max_tool_calls": 20, "execution_timeout_seconds": 120.0}
    assert enabled["models"]["voiceprint"]["model"] == "vision-model"
    assert enabled["models"]["speaker_embedding"]["model"] == "campplus"


@pytest.mark.parametrize("total,ready_counts", [(4, (1, 2)), (5, (2, 3)), (1, (0, 1))])
def test_scheduler_releases_at_ceiling_half_only_when_enabled(total, ready_counts):
    def schedule(ready):
        return ChunkScheduleSnapshot(tuple(
            ChunkScheduleEntry(str(index), index, ChunkTaskStatus.WAITING,
                               understanding_committed=index < ready)
            for index in range(total)
        ), 0)

    runtime = SimpleNamespace(
        load_chunk_schedule=AsyncMock(side_effect=[schedule(count) for count in ready_counts]),
        wait_for_video_change=AsyncMock(),
    )
    state = {"video_parse_task_id": "task", "inputs": _voice_inputs()}
    assert asyncio.run(ChunkScheduler(runtime=runtime).run(state)) == state
    assert runtime.load_chunk_schedule.await_count == 2
    runtime.wait_for_video_change.assert_awaited_once()


def test_disabled_scheduler_still_waits_for_all_understanding():
    runtime = SimpleNamespace(
        load_chunk_schedule=AsyncMock(side_effect=[
            ChunkScheduleSnapshot(tuple(
                ChunkScheduleEntry(str(index), index, ChunkTaskStatus.WAITING,
                                   understanding_committed=index < ready)
                for index in range(4)
            ), 0) for ready in (2, 4)
        ]), wait_for_video_change=AsyncMock(),
    )
    state = {"video_parse_task_id": "task", "inputs": _inputs()}
    asyncio.run(ChunkScheduler(runtime=runtime).run(state))
    runtime.wait_for_video_change.assert_awaited_once()


def test_main_graph_prepares_voiceprints_before_waiting_for_completion():
    calls = []
    lease = _lease()
    runtime = SimpleNamespace(
        load_video_run=AsyncMock(return_value=VideoRunSnapshot(
            "task", VideoTaskStatus.PARSING, _voice_inputs(), 20_000, lease,
        )),
        load_chunk_schedule=AsyncMock(return_value=ChunkScheduleSnapshot((
            ChunkScheduleEntry("chunk-0", 0, ChunkTaskStatus.WAITING, understanding_committed=True),
            ChunkScheduleEntry("chunk-1", 1, ChunkTaskStatus.QUEUED),
        ), 0)),
        load_voiceprint_input=AsyncMock(return_value=None),
    )

    async def finish(task_id, lease):
        assert runtime.load_voiceprint_input.await_count == 1
        calls.append("complete")
        return VideoTaskStatus.COMPLETE

    runtime.advance_video_task = finish
    state = asyncio.run(ParseAgent(runtime=runtime).InvokeVideoParse("task"))
    assert state["status"] == VideoTaskStatus.COMPLETE
    assert calls == ["complete"]
    runtime.load_voiceprint_input.assert_awaited_once_with("task", lease)


def test_prepared_voiceprint_skips_model_and_notify():
    runtime = SimpleNamespace(
        load_voiceprint_input=AsyncMock(return_value=None),
        generate_voiceprint_preparation=AsyncMock(), notify_voiceprint_prepared=AsyncMock(),
    )
    asyncio.run(VoiceprintAgent(runtime=runtime).InvokeVoiceprint("task", _lease()))
    runtime.generate_voiceprint_preparation.assert_not_awaited()
    runtime.notify_voiceprint_prepared.assert_not_awaited()


def test_voiceprint_notifies_only_after_durable_ready_and_keeps_sources_readonly():
    calls = []

    async def generate(snapshot, *, canvas, prompt):
        assert canvas.get("sources", "issued-source")["start_ms"] == 1000
        assert "strictly greater than half" in prompt
        assert "non-null speaker" in prompt
        with pytest.raises(AgentToolFeedbackError, match="read-only"):
            canvas.put("sources", "issued-source", {"start_ms": 0})
        calls.append("generate")
        return {"complete": True}

    async def ensure(snapshot):
        calls.append("ensure")

    async def notify(snapshot):
        calls.append("notify")

    runtime = SimpleNamespace(
        load_voiceprint_input=AsyncMock(return_value=_snapshot()),
        generate_voiceprint_preparation=generate, ensure_voiceprint_prepared=ensure,
        notify_voiceprint_prepared=notify,
    )
    asyncio.run(VoiceprintAgent(runtime=runtime).InvokeVoiceprint("task", _lease()))
    assert calls == ["generate", "ensure", "notify"]


@pytest.mark.parametrize("error", [RuntimeError("database failed"), asyncio.CancelledError()])
def test_text_completion_cannot_bypass_ready_and_errors_propagate(error):
    runtime = SimpleNamespace(
        load_voiceprint_input=AsyncMock(return_value=_snapshot()),
        generate_voiceprint_preparation=AsyncMock(return_value={"complete": True}),
        ensure_voiceprint_prepared=AsyncMock(side_effect=error),
        notify_voiceprint_prepared=AsyncMock(),
    )
    with pytest.raises(type(error)):
        asyncio.run(VoiceprintAgent(runtime=runtime).InvokeVoiceprint("task", _lease()))
    runtime.notify_voiceprint_prepared.assert_not_awaited()


def test_four_host_bound_tools_and_comparison_preserve_ms_and_real_failures():
    calls = []
    canvas = ContextCanvas("voiceprint:task")
    canvas.put("sources", "issued", {"start_ms": 47100, "end_ms": 48800}, readonly=True)
    tools = {tool.name: tool for tool in build_tools(
        canvas, get_speaker_list=lambda: {"speaker_list_size": 0, "required_count": 0},
        register_voiceprint=lambda **values: calls.append(values) or {"registered": True},
        finish_voiceprint_preparation=lambda **values: {"ready": True},
    )}
    assert set(tools) == {
        "get_context_canvas", "get_speaker_list", "register_voiceprint", "finish_voiceprint_preparation",
    }
    assert tools["get_speaker_list"].execute().payload["required_count"] == 0
    assert tools["get_context_canvas"].execute(
        collection="sources", reference="issued", section="value",
    ).success is True
    tools["register_voiceprint"].execute(
        source_ref="issued", speaker="爽世", start_ms=47100, end_ms=48800, selection_basis="已确认来源",
    )
    assert calls[-1]["start_ms"] == 47100
    assert tools["finish_voiceprint_preparation"].execute(complete=True).payload["ready"] is True

    def fail(**values):
        assert values == {"start_ms": 47100, "end_ms": 48800}
        raise OSError("audio extraction failed")

    tool = build_compare_speaker_tool(compare_speaker=fail)
    with pytest.raises(OSError, match="audio extraction failed"):
        tool.execute(start_ms=47100, end_ms=48800)


def test_tool_argument_contract_forbids_paths_vectors_and_reverse_time():
    values = {
        "source_ref": "issued", "speaker": "爽世", "start_ms": 47100,
        "end_ms": 48800, "selection_basis": "已确认来源",
    }
    assert RegisterVoiceprintArguments.model_validate(values).start_ms == 47100
    for extra in ({"path": "/tmp/arbitrary.wav"}, {"vector": [1.0] * 192}):
        with pytest.raises(ValidationError, match="Extra inputs"):
            RegisterVoiceprintArguments.model_validate({**values, **extra})
    with pytest.raises(ValidationError, match="exceed"):
        CompareSpeakerArguments(start_ms=2000, end_ms=1000)
