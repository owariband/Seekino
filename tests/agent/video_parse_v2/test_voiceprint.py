from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent.utils.context_canvas import ContextCanvas
from agent.utils.errors import AgentToolFeedbackError
from agent.video_parse_v2 import ParseAgent
from agent.video_parse_v2.input import ParseInput
from agent.video_parse_v2.prompts import build_refine_prompt
from agent.video_parse_v2.refine.agent import IntervalRefine
from agent.video_parse_v2.scheduler import ChunkScheduler
from agent.video_parse_v2.state import (
    ChunkScheduleEntry, ChunkScheduleSnapshot, ChunkTaskStatus, VideoRunSnapshot, VideoTaskStatus,
)
from agent.video_parse_v2.voiceprint import (
    CompareSpeakerArguments, RegisterVoiceprintArguments, UnregisterVoiceprintArguments, VoiceprintAgent,
    build_compare_speaker_tool, build_tools,
)
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseContextCanvas
from engine.parsev2.voiceprint import Voiceprint, VoiceprintReference
from tests.agent.video_parse_v2.test_framework import _inputs, _lease, _result
from tests.agent.video_parse_v2.test_context_canvas import _voice_snapshot


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


@pytest.mark.parametrize("mode", ["flag", "prefill"])
def test_scoring_mode_needs_only_speaker_encoder_without_legacy_agent_settings(mode):
    values = _voice_inputs().model_dump(mode="json")
    config = values["parse_config"]
    config["voiceprint_mode"] = mode
    config.pop("voiceprint")
    config["models"].pop("voiceprint")
    parsed = ParseInput.model_validate(values)
    assert parsed.parse_config.voiceprint_enabled is True
    assert parsed.parse_config.voiceprint is None
    assert parsed.parse_config.models.voiceprint is None
    assert parsed.parse_config.voiceprint_mode == mode
    config["models"].pop("speaker_embedding")
    with pytest.raises(ValidationError, match="speaker_embedding"):
        ParseInput.model_validate(values)


def test_voiceprint_mode_defaults_off_is_omitted_and_rejects_unknown_values():
    values = _inputs().model_dump(mode="json")
    assert "voiceprint_mode" not in values["parse_config"]
    assert ParseInput.model_validate(values).parse_config.voiceprint_mode == "off"
    values["parse_config"]["voiceprint_mode"] = "automatic"
    with pytest.raises(ValidationError):
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


@pytest.mark.parametrize("total,ready_counts", [
    (4, (2, 3)), (5, (3, 4)), (24, (17, 18)), (2, (1, 2)), (1, (0, 1)),
])
def test_scheduler_releases_at_ceiling_three_quarters_only_when_enabled(total, ready_counts):
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
            ), 0) for ready in (3, 4)
        ]), wait_for_video_change=AsyncMock(),
    )
    state = {"video_parse_task_id": "task", "inputs": _inputs()}
    asyncio.run(ChunkScheduler(runtime=runtime).run(state))
    runtime.wait_for_video_change.assert_awaited_once()


def test_scoring_scheduler_waits_for_all_terminal_including_permanent_failure():
    values = _voice_inputs().model_dump(mode="json")
    values["parse_config"]["voiceprint_mode"] = "flag"
    inputs = ParseInput.model_validate(values)
    completed = tuple(ChunkScheduleEntry(str(index), index, ChunkTaskStatus.WAITING,
                                         understanding_committed=True) for index in range(3))
    runtime = SimpleNamespace(
        load_chunk_schedule=AsyncMock(side_effect=[
            ChunkScheduleSnapshot((*completed, ChunkScheduleEntry("last", 3, ChunkTaskStatus.QUEUED)), 0),
            ChunkScheduleSnapshot((*completed, ChunkScheduleEntry("last", 3, ChunkTaskStatus.FAILED)), 0),
        ]),
        wait_for_video_change=AsyncMock(),
    )
    state = {"video_parse_task_id": "task", "inputs": inputs}
    assert asyncio.run(ChunkScheduler(runtime=runtime).run(state)) == state
    assert runtime.load_chunk_schedule.await_count == 2
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
            ChunkScheduleEntry("chunk-1", 1, ChunkTaskStatus.WAITING, understanding_committed=True),
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
        assert "suspect_references" in prompt and "replace_source_ref" in prompt
        assert "unregister_voiceprint" in prompt and "skip_reasons cannot bypass" in prompt
        assert "early, middle and late" in prompt and "sequence_no" in prompt
        assert "one reliable utterance per distinct available chunk" in prompt
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


def test_five_host_bound_tools_and_comparison_preserve_ms_and_real_failures():
    calls = []
    canvas = ContextCanvas("voiceprint:task")
    canvas.put("sources", "issued", {"start_ms": 47100, "end_ms": 48800}, readonly=True)
    tools = {tool.name: tool for tool in build_tools(
        canvas, get_speaker_list=lambda: {"speaker_list_size": 0, "required_count": 0},
        register_voiceprint=lambda **values: calls.append(values) or {"registered": True},
        unregister_voiceprint=lambda **values: calls.append(values) or {"removed": True},
        finish_voiceprint_preparation=lambda **values: {"ready": True},
    )}
    assert set(tools) == {
        "get_context_canvas", "get_speaker_list", "register_voiceprint", "unregister_voiceprint",
        "finish_voiceprint_preparation",
    }
    assert tools["get_speaker_list"].execute().payload["required_count"] == 0
    assert tools["get_context_canvas"].execute(
        collection="sources", reference="issued", section="value",
    ).success is True
    tools["register_voiceprint"].execute(
        source_ref="issued", speaker="爽世", start_ms=47100, end_ms=48800, selection_basis="已确认来源",
        replace_source_ref="old-source",
    )
    assert calls[-1]["start_ms"] == 47100 and calls[-1]["replace_source_ref"] == "old-source"
    assert tools["unregister_voiceprint"].execute(source_ref="old-source").payload["removed"] is True
    assert calls[-1] == {"source_ref": "old-source"}
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
    assert RegisterVoiceprintArguments.model_validate(values).replace_source_ref is None
    assert RegisterVoiceprintArguments.model_validate({**values, "replace_source_ref": "old"}).replace_source_ref == "old"
    with pytest.raises(ValidationError, match="at least 1 character"):
        RegisterVoiceprintArguments.model_validate({**values, "replace_source_ref": ""})
    assert UnregisterVoiceprintArguments(source_ref="old").source_ref == "old"
    with pytest.raises(ValidationError, match="Extra inputs"):
        UnregisterVoiceprintArguments.model_validate({"source_ref": "old", "path": "/tmp/arbitrary.wav"})
    for extra in ({"path": "/tmp/arbitrary.wav"}, {"vector": [1.0] * 192}):
        with pytest.raises(ValidationError, match="Extra inputs"):
            RegisterVoiceprintArguments.model_validate({**values, **extra})
    with pytest.raises(ValidationError, match="exceed"):
        CompareSpeakerArguments(start_ms=2000, end_ms=1000)


@pytest.mark.parametrize("mode,band,speaker", [
    ("flag", "CONFLICT", "爱音"), ("prefill", "ACCEPT", "爽世"),
])
def test_refine_prompt_first_checkpoint_exposes_voice_conflicts_and_required_decision_gate(mode, band, speaker):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot(mode, band))
    reference = canvas.items("events")[0][0]
    prompt = build_refine_prompt(start_sequence_no=0, chunk_count=1, canvas=canvas)
    catalog = json.loads(prompt.splitlines()[-1])["collections"]["events"]
    assert catalog == [{
        "reference": reference, "member_ref": "member", "event_type": "DIALOGUE",
        "speaker": speaker, "voice": {"top1": "爽世", "gap": 0.2,
                                    "band": band, "conflict": band == "CONFLICT"},
    }]
    assert "op=voice_decision" in prompt and "decision=keep|voice_top1|other|null" in prompt
    assert "reason of 1-300 characters" in prompt and "this window's write bounds" in prompt
    assert "keep equals understanding_speaker" in prompt and "voice_top1 equals candidates[0].speaker" in prompt
    assert "other is a named speaker different from both" in prompt and "null means speaker is null" in prompt
    assert "Earlier windows' committed decisions are information only" in prompt
    assert "finish_voiceprint_review" in prompt and "before CanvasCompletion" in prompt
    assert "missing or inconsistent event_ref" in prompt
    assert "Conversational context alone does not override a voice disagreement" in prompt
    assert "speaker being addressed by name or visibly speaking" in prompt


def test_voice_off_refine_prompt_and_commit_keep_historical_call_contract():
    snapshot = _voice_snapshot("off")

    async def generate(_snapshot, *, canvas, prompt):
        assert "op=voice_decision" not in prompt and "finish_voiceprint_review" not in prompt
        assert "Required voice review lists" not in prompt
        checkpoint = json.loads(prompt.splitlines()[-1])
        assert set(checkpoint["collections"]["events"][0]) == {"reference", "member_ref", "event_type"}
        return CanvasCompletion(complete=True)

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(side_effect=generate),
        commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    runtime.commit_interval_refinement.assert_awaited_once()
    assert runtime.commit_interval_refinement.await_args.args[0] is snapshot
    assert len(runtime.commit_interval_refinement.await_args.args) == 2
    assert runtime.commit_interval_refinement.await_args.kwargs == {}
    assert runtime.commit_interval_refinement.await_args.args[1].updates == []
    runtime.notify_interval_committed.assert_awaited_once_with(snapshot)


@pytest.mark.parametrize("mode", ["flag", "prefill"])
def test_refine_commits_canvas_decision_records_with_complete_provenance(mode):
    snapshot = _voice_snapshot(mode, "CONFLICT")
    original = deepcopy(snapshot)

    async def generate(_snapshot, *, canvas, prompt):
        reference = canvas.items("events")[0][0]
        canvas.write(role="refine", batch_id="resolve-voice", expected_revision=canvas.revision, operations=[
            {"op": "persons", "member_ref": "member", "action": "add", "values": ["爽世"]},
            {"op": "event_array", "event_ref": reference, "field": "participants", "action": "add",
             "values": ["爽世"]},
            {"op": "patch_event", "event_ref": reference, "fields": {"speaker": "爽世"}},
            {"op": "voice_decision", "event_ref": reference, "decision": "voice_top1",
             "reason": "The target is visibly speaking."},
        ])
        assert canvas.finish_voiceprint_review()["ready"] is True
        assert canvas._initial["member"].model_dump(mode="json") == original["member_contents"]["member"]
        return CanvasCompletion(complete=True)

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(side_effect=generate),
        commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    records = runtime.commit_interval_refinement.await_args.kwargs["voiceprint_decisions"]
    assert len(records) == 1
    assert records[0] == {
        "event_ref": records[0]["event_ref"], "member_chunk_id": "member",
        "start_ms": 1000, "end_ms": 3000, "understanding_speaker": "爱音",
        "voice_top1": "爽世", "gap": 0.2, "band": "CONFLICT", "decision": "voice_top1",
        "reason": "The target is visibly speaking.", "final_speaker": "爽世",
    }
    assert runtime.commit_interval_refinement.await_args.args[1].updates[0].timeline[0].speaker == "爽世"
    runtime.notify_interval_committed.assert_awaited_once_with(snapshot)
    assert snapshot == original


def test_refine_refresh_requires_new_canvas_decisions_after_a_commit_conflict():
    snapshots = [_voice_snapshot("flag", "CONFLICT"), _voice_snapshot("flag", "CONFLICT")]
    references = []

    async def generate(_snapshot, *, canvas, prompt):
        reference = canvas.items("events")[0][0]
        references.append(reference)
        assert canvas.voiceprint_review_status()["missing"] == [reference]
        decision, speaker = ("voice_top1", "爽世") if len(references) == 1 else ("keep", "爱音")
        canvas.write(role="refine", batch_id="resolve", expected_revision=canvas.revision, operations=[
            {"op": "persons", "member_ref": "member", "action": "add", "values": [speaker]},
            {"op": "event_array", "event_ref": reference, "field": "participants", "action": "add",
             "values": [speaker]},
            {"op": "patch_event", "event_ref": reference, "fields": {"speaker": speaker}},
            {"op": "voice_decision", "event_ref": reference, "decision": decision,
             "reason": f"Fresh window review {len(references)}."},
        ])
        assert canvas.finish_voiceprint_review()["ready"] is True
        return CanvasCompletion(complete=True)

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(side_effect=snapshots),
        generate_interval_refinement=AsyncMock(side_effect=generate),
        commit_interval_refinement=AsyncMock(side_effect=[False, True]), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    assert len(set(references)) == 2
    assert [call.kwargs["voiceprint_decisions"][0]["decision"]
            for call in runtime.commit_interval_refinement.await_args_list] == ["voice_top1", "keep"]
    assert [call.kwargs["voiceprint_decisions"][0]["event_ref"]
            for call in runtime.commit_interval_refinement.await_args_list] == references
    runtime.notify_interval_committed.assert_awaited_once_with(snapshots[1])


def test_refine_receipt_without_voice_gate_does_not_export_or_commit_a_draft():
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=_voice_snapshot("flag", "CONFLICT")),
        generate_interval_refinement=AsyncMock(return_value=CanvasCompletion(complete=True)),
        commit_interval_refinement=AsyncMock(), notify_interval_committed=AsyncMock(),
    )
    with pytest.raises(AgentToolFeedbackError, match="review gate"):
        asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    runtime.commit_interval_refinement.assert_not_awaited()
    runtime.notify_interval_committed.assert_not_awaited()
