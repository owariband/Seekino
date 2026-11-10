from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent.video_parse_v2.refine.agent import IntervalRefine
from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import (
    IntervalRefinementResult, RefinementResultError, normalize_refinement_response,
    validate_result_time_range,
)
from tests.agent.video_parse_v2.test_context_canvas import _voice_snapshot


def response(snapshot):
    canvas = ParseContextCanvas.from_refine(snapshot)
    return {"members": [{"sequence_no": seq, **canvas.result(ref).model_dump(
        mode="json", exclude={"timeline": {"__all__": {"speaker_source"}}},
    )} for ref, seq in snapshot["member_sequence_nos"].items()]}


@pytest.mark.parametrize("invalid", ["schema", "missing", "duplicate", "foreign"])
def test_result_gate_returns_specific_error_and_only_commits_corrected_result(invalid):
    snapshot = _voice_snapshot()
    config = snapshot["inputs"].parse_config
    snapshot["inputs"] = snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(
        update={"execution": config.execution.model_copy(update={"max_retry_count": 1})},
    )})
    valid = response(snapshot)
    bad = deepcopy(valid)
    if invalid == "schema":
        bad["members"][0]["timeline"][0]["participants"] = []
    elif invalid == "missing":
        bad["members"] = []
    elif invalid == "duplicate":
        bad["members"] *= 2
    else:
        bad["members"][0]["sequence_no"] = 12
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(side_effect=[bad, valid]),
        commit_interval_refinement=AsyncMock(return_value=True),
        notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    calls = runtime.generate_interval_refinement.await_args_list
    assert len(calls) == 2
    assert "result gate rejected" in calls[1].kwargs["prompt"]
    assert "members" in calls[1].kwargs["prompt"]
    assert calls[1].kwargs["remaining_seconds"] <= calls[0].kwargs["remaining_seconds"]
    runtime.commit_interval_refinement.assert_awaited_once()
    runtime.notify_interval_committed.assert_awaited_once()


def test_invalid_results_exhaust_existing_retry_policy_without_commit():
    snapshot = _voice_snapshot()
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(return_value={"members": []}),
        commit_interval_refinement=AsyncMock(), notify_interval_committed=AsyncMock(),
    )
    with pytest.raises(ValidationError):
        asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    assert runtime.generate_interval_refinement.await_count == snapshot["inputs"].parse_config.execution.max_retry_count + 1
    runtime.commit_interval_refinement.assert_not_awaited()


def test_complete_json_preserves_host_provenance_and_relabels_changed_audio():
    snapshot = _voice_snapshot()
    canvas = ParseContextCanvas.from_refine(snapshot)
    value = response(snapshot)
    value["members"][0]["timeline"][0]["speaker_source"] = "understanding"
    accepted = canvas.accept_refinement(IntervalRefinementResult.model_validate(value))
    assert accepted.updates[0].timeline[0].speaker_source == "voice_default"
    value["members"][0]["timeline"][0]["start_seconds"] += 10
    accepted = canvas.accept_refinement(IntervalRefinementResult.model_validate(value))
    assert accepted.updates[0].timeline[0].speaker_source == "refine_edit"
    assert canvas.result("member").timeline[0].start_seconds == 1000


def test_whole_video_asr_dialogue_can_cross_window_but_not_video():
    snapshot = _voice_snapshot("off")
    snapshot["dialogue_bounds"] = (0, 25_000)
    canvas = ParseContextCanvas.from_refine(snapshot)
    value = response(snapshot)
    event = value["members"][0]["timeline"][0]
    event.update(start_seconds=19_000, end_seconds=21_440)
    accepted = canvas.accept_refinement(IntervalRefinementResult.model_validate(value))
    assert accepted.updates[0].timeline[0].end_seconds == 21_440
    event["end_seconds"] = 25_001
    with pytest.raises(RefinementResultError, match="time range"):
        canvas.accept_refinement(IntervalRefinementResult.model_validate(value))
    visual = VideoParseResult(persons=[], timeline=[{
        "event_type": "ACTION", "start_seconds": 19_000, "end_seconds": 21_440,
        "description": "outside frames", "participants": [], "visible_persons": [],
    }])
    with pytest.raises(ValueError, match="time range"):
        validate_result_time_range(visual, start_seconds=0, end_seconds=20_000, dialogue_bounds=(0,25_000))


def test_refine_conflict_refreshes_snapshot_without_resetting_deadline():
    snapshot = _voice_snapshot()
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(return_value=response(snapshot)),
        commit_interval_refinement=AsyncMock(side_effect=[False, True]),
        notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    assert runtime.load_refine_input.await_count == 2
    calls = runtime.generate_interval_refinement.await_args_list
    assert len(calls) == 2 and calls[1].kwargs["remaining_seconds"] <= calls[0].kwargs["remaining_seconds"]
    runtime.notify_interval_committed.assert_awaited_once()


@pytest.mark.parametrize("error", [asyncio.CancelledError(), RuntimeError("lease lost")])
def test_refine_control_failure_does_not_enter_schema_repair_or_commit(error):
    snapshot = _voice_snapshot()
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(side_effect=error),
        commit_interval_refinement=AsyncMock(), notify_interval_committed=AsyncMock(),
    )
    with pytest.raises(type(error)):
        asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    runtime.generate_interval_refinement.assert_awaited_once()
    runtime.commit_interval_refinement.assert_not_awaited()


def test_refine_response_after_deadline_never_commits(monkeypatch):
    import agent.video_parse_v2.refine.agent as module
    ticks = iter([0, 0, 100_000])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    snapshot = _voice_snapshot()
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(return_value=response(snapshot)),
        commit_interval_refinement=AsyncMock(), notify_interval_committed=AsyncMock(),
    )
    with pytest.raises(TimeoutError, match="deadline"):
        asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    runtime.commit_interval_refinement.assert_not_awaited()


def test_provider_schema_stays_canonical_but_invalid_payload_reaches_business_gate():
    from agent.video_parse_v2.refine.response import RefineModelResponse

    bad = {"members": [{"sequence_no": 0, "persons": [], "timeline": [], "unexpected": "keep me"}]}
    from agent.video_parse_v2.refine.response import ClipRefineModelResponse
    assert ClipRefineModelResponse.model_json_schema() == IntervalRefinementResult.model_json_schema()
    assert "camera_ref" not in json.dumps(RefineModelResponse.model_json_schema())
    received = RefineModelResponse.model_validate_json(json.dumps(bad))
    assert received.root == bad
    with pytest.raises(ValidationError, match="unexpected"):
        IntervalRefinementResult.model_validate(received.root)


def test_repair_preserves_complete_rejected_payload_and_only_the_latest_attempt():
    from agent.video_parse_v2.refine.response import RefineModelResponse

    snapshot = _voice_snapshot()
    config = snapshot["inputs"].parse_config
    snapshot["inputs"] = snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(
        update={"execution": config.execution.model_copy(update={"max_retry_count": 2})},
    )})
    valid = response(snapshot)
    valid["members"][0]["timeline"][0]["description"] = "preserved improvement " + "long evidence " * 400
    first = deepcopy(valid)
    first["members"][0]["timeline"][0]["participants"] = []
    second = deepcopy(valid)
    second["members"][0]["timeline"][0]["end_seconds"] = 0
    candidates = [first, second, valid]
    seen = []

    async def generate(_snapshot, *, prompt, remaining_seconds):
        index = len(seen)
        if index:
            feedback = json.loads(prompt.split("\nRejected response JSON:\n", 1)[1])
            assert feedback == candidates[index - 1]
            assert prompt.count("\nRejected response JSON:\n") == 1
            runtime.commit_interval_refinement.assert_not_awaited()
        seen.append(prompt)
        # Exercise the exact model_validate_json boundary used by adapters.
        return RefineModelResponse.model_validate_json(json.dumps(candidates[index]))

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=generate,
        commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    assert len(seen) == 3
    committed = runtime.commit_interval_refinement.await_args.args[1]
    assert committed.updates[0].timeline[0].description == valid["members"][0]["timeline"][0]["description"]


def test_malformed_json_returns_full_raw_text_for_repair():
    from agent.video_parse_v2.refine.response import RefineModelResponse

    snapshot = _voice_snapshot()
    config = snapshot["inputs"].parse_config
    snapshot["inputs"] = snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(
        update={"execution": config.execution.model_copy(update={"max_retry_count": 1})},
    )})
    malformed = '{"members":[{"sequence_no":0,"persons":['
    prompts = []

    async def generate(_snapshot, *, prompt, remaining_seconds):
        prompts.append(prompt)
        if len(prompts) == 1:
            return RefineModelResponse.model_validate_json(malformed)
        assert json.loads(prompt.split("\nRejected response JSON:\n", 1)[1]) == malformed
        return RefineModelResponse.model_validate(response(snapshot))

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot), generate_interval_refinement=generate,
        commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    assert len(prompts) == 2
    runtime.commit_interval_refinement.assert_awaited_once()


def test_refine_stably_sorts_without_mutation_or_another_model_call():
    from agent.video_parse_v2.refine.response import RefineModelResponse

    snapshot = _voice_snapshot()
    value = response(snapshot)
    timeline = value["members"][0]["timeline"]
    early = {"event_type": "ACTION", "start_seconds": 0, "end_seconds": 500,
             "description": "earlier", "participants": [], "visible_persons": []}
    tied = {**early, "description": "same time, preserve relative order"}
    timeline.extend([early, tied])
    original = deepcopy(value)
    with pytest.raises(ValidationError, match="ordered"):
        VideoParseResult.model_validate({k: v for k, v in value["members"][0].items() if k != "sequence_no"})
    normalized = IntervalRefinementResult.model_validate(normalize_refinement_response(value))
    events = normalized.members[0].timeline
    assert [e.description for e in events[:2]] == [early["description"], tied["description"]]
    assert value == original
    assert [e.model_dump(mode="json", exclude_unset=True) for e in events] == sorted(
        original["members"][0]["timeline"], key=lambda e: e["start_seconds"],
    )
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(return_value=RefineModelResponse.model_validate(value)),
        commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    runtime.generate_interval_refinement.assert_awaited_once()
    runtime.commit_interval_refinement.assert_awaited_once()


@pytest.mark.parametrize("damage", ["end", "speaker", "boolean_time", "unknown_field"])
def test_sorting_never_accepts_other_invalid_event_fields(damage):
    value = response(_voice_snapshot())
    event = value["members"][0]["timeline"][0]
    if damage == "end":
        event["end_seconds"] = event["start_seconds"]
    elif damage == "speaker":
        event["speaker"] = "unknown person"
    elif damage == "boolean_time":
        event["start_seconds"] = True
    else:
        event["silently_drop_me"] = "forbidden"
    with pytest.raises(ValidationError):
        IntervalRefinementResult.model_validate(value)


def test_repair_locations_refer_to_the_normalized_payload_supplied_to_model():
    from agent.video_parse_v2.refine.response import RefineModelResponse

    snapshot = _voice_snapshot()
    config = snapshot["inputs"].parse_config
    snapshot["inputs"] = snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(
        update={"execution": config.execution.model_copy(update={"max_retry_count": 1})},
    )})
    bad = response(snapshot)
    bad["members"][0]["timeline"][0]["participants"] = []
    bad["members"][0]["timeline"].append({
        "event_type": "ACTION", "start_seconds": 0, "end_seconds": 500,
        "description": "earlier", "participants": [], "visible_persons": [],
    })
    prompts = []

    async def generate(_snapshot, *, prompt, remaining_seconds):
        prompts.append(prompt)
        if len(prompts) == 1:
            return RefineModelResponse.model_validate(bad)
        assert "members.0.timeline.1.DIALOGUE" in prompt
        rejected = json.loads(prompt.split("\nRejected response JSON:\n", 1)[1])
        assert rejected == normalize_refinement_response(bad)
        assert rejected["members"][0]["timeline"][1] == bad["members"][0]["timeline"][0]
        return RefineModelResponse.model_validate(response(snapshot))

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot), generate_interval_refinement=generate,
        commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    runtime.commit_interval_refinement.assert_awaited_once()


def test_commit_conflict_retains_candidate_and_specific_reason_with_fresh_input():
    from agent.video_parse_v2.refine.input import RefineInputChanged

    snapshot = _voice_snapshot()
    latest = deepcopy(snapshot)
    latest["member_versions"]["member"] += 1
    latest["member_contents"]["member"]["timeline"][0]["description"] = "neighbor committed correction"
    candidate = response(snapshot)
    candidate["members"][0]["timeline"][0]["description"] = "my uncommitted correction"
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(side_effect=[snapshot, latest]),
        generate_interval_refinement=AsyncMock(side_effect=[candidate, response(latest)]),
        commit_interval_refinement=AsyncMock(side_effect=[
            RefineInputChanged("timeline_structure_changed", context={"first_mismatch": 2}), True,
        ]), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    calls = runtime.generate_interval_refinement.await_args_list
    prompt = calls[1].kwargs["prompt"]
    assert "neighbor committed correction" in prompt
    assert '"first_mismatch": 2' in prompt and "timeline_structure_changed" in prompt
    assert json.loads(prompt.split("\nPrevious uncommitted candidate JSON:\n")[1]) == candidate
    assert calls[1].kwargs["remaining_seconds"] < calls[0].kwargs["remaining_seconds"]
    runtime.notify_interval_committed.assert_awaited_once()


def test_update_during_schema_repair_keeps_rejected_json_and_field_location():
    from agent.video_parse_v2.refine.input import RefineInputChanged

    snapshot = _voice_snapshot()
    config = snapshot["inputs"].parse_config
    snapshot["inputs"] = snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(
        update={"execution": config.execution.model_copy(update={"max_retry_count": 1})},
    )})
    bad = response(snapshot)
    bad["members"][0]["timeline"][0]["participants"] = []
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(side_effect=[
            bad, RefineInputChanged("member_content_updated", context={"changed_versions": {"member": 2}}),
            response(snapshot),
        ]), commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    calls = runtime.generate_interval_refinement.await_args_list
    prompt = calls[2].kwargs["prompt"]
    assert "members.0.timeline.0" in prompt
    assert json.loads(prompt.split("\nPrevious uncommitted candidate JSON:\n")[1]) == bad
    assert calls[2].kwargs["remaining_seconds"] < calls[0].kwargs["remaining_seconds"]
    runtime.commit_interval_refinement.assert_awaited_once()


def test_content_updates_cannot_extend_the_original_deadline(monkeypatch):
    from agent.video_parse_v2.refine.input import RefineInputChanged
    import agent.video_parse_v2.refine.agent as module

    ticks = iter([0, 0, 100_000, 100_000])
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    snapshot = _voice_snapshot()
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(side_effect=RefineInputChanged("member_content_updated")),
        commit_interval_refinement=AsyncMock(), notify_interval_committed=AsyncMock(),
    )
    with pytest.raises(TimeoutError, match="deadline"):
        asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    runtime.generate_interval_refinement.assert_awaited_once()
    runtime.commit_interval_refinement.assert_not_awaited()


def test_responses_adapter_preserves_invalid_json_for_refine_repair():
    from agent.video_parse_v2.refine.response import RefineModelResponse
    from gateway.multimodal import TextInput
    from gateway.openai.multimodal import DeepSeekMultimodalModel

    snapshot = _voice_snapshot()
    config = snapshot["inputs"].parse_config
    snapshot["inputs"] = snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(
        update={"execution": config.execution.model_copy(update={"max_retry_count": 1})},
    )})
    valid = response(snapshot)
    raw = json.dumps(valid)[:-1] + ",}"
    outputs = iter([raw, json.dumps(valid)])
    prompts = []

    def create(**kwargs):
        prompts.append(kwargs["input"][0]["content"][0]["text"])
        return SimpleNamespace(status="completed", output_text=next(outputs), output=[])

    model = DeepSeekMultimodalModel(
        "deepseek-flash", stream=False, base_url="https://example.test", api_key="test",
        max_output_tokens=10000, request_timeout_seconds=600,
        client=SimpleNamespace(responses=SimpleNamespace(create=create)),
    )

    async def generate(snapshot, *, prompt, remaining_seconds):
        return model.generate([TextInput(prompt)], schema_name="IntervalRefinementResult",
                              result_type=RefineModelResponse, request_timeout_seconds=remaining_seconds)

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot), generate_interval_refinement=generate,
        commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    assert len(prompts) == 2
    assert "line 1 column" in prompts[1]
    rejected = prompts[1].split("Rejected response JSON:\n", 1)[1]
    assert json.loads(rejected) == raw
    runtime.commit_interval_refinement.assert_awaited_once()
