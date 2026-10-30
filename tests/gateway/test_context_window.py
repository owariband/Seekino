import json

import pytest

from gateway.agent_model.context import (
    ContextWindowController,
    ContextWindowOverflow,
    ContextWindowPolicy,
    measure_context,
    preflight_context,
)


def test_video_input_budget_excludes_inline_base64_and_keeps_media_reserve() -> None:
    def measurement(data):
        return measure_context([{"role": "user", "content": [{
            "type": "input_video", "video_url": f"data:video/mp4;base64,{data}", "fps": 0.5,
        }]}], [])

    small = measurement("AAAA")
    large = measurement("A" * 100000)
    assert large == small
    assert large.characters < 200
    assert large.estimated_tokens >= 2048


@pytest.mark.parametrize("frame_count", [180, 198])
def test_preflight_admits_parse_frames_without_changing_compaction_estimates(frame_count) -> None:
    policy = ContextWindowPolicy(128_000, 8_192, 2_048, 90_000, 60_000)
    messages = [{"role": "user", "content": [
        {"type": "input_image", "image_url": f"data:image/png;base64,frame-{index}"}
        for index in range(frame_count)
    ]}]
    estimate = measure_context(messages, {})

    admitted = preflight_context(
        messages, {}, policy=policy, maximum_input_characters=400_000,
        fixed_input_tokens=13_000, label="parse frames",
    )

    assert estimate.estimated_tokens > policy.maximum_input_tokens
    assert admitted.estimated_tokens < policy.maximum_input_tokens
    assert admitted.characters == estimate.characters
    controller = ContextWindowController(
        policy=policy, maximum_input_characters=400_000, label="parse frames",
    )
    update = controller.prepare(messages, contract={})
    assert update.before == estimate
    assert update.messages[0] == messages[0]


def _assistant_tool_call(call_id: str, name: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }],
    }


def _tool_result(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _policy() -> ContextWindowPolicy:
    return ContextWindowPolicy(
        context_window_tokens=10_000,
        output_reserve_tokens=1_000,
        safety_margin_tokens=1_000,
        auto_compact_tokens=1_500,
        compact_target_tokens=750,
    )


def test_policy_reserves_output_and_safety_tokens() -> None:
    policy = _policy()

    assert policy.maximum_input_tokens == 8_000

    with pytest.raises(ValueError, match="maximum input budget"):
        ContextWindowPolicy(
            context_window_tokens=4_000,
            output_reserve_tokens=1_000,
            safety_margin_tokens=1_000,
            auto_compact_tokens=2_001,
            compact_target_tokens=500,
        )


def test_preflight_adds_fixed_tokens_without_using_the_compaction_threshold() -> None:
    messages = [{"role": "user", "content": "short request"}]
    contract = {
        "tools": [{
            "name": "lookup",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }],
    }
    payload = measure_context(messages, contract)

    measurement = preflight_context(
        messages,
        contract,
        policy=_policy(),
        maximum_input_characters=None,
        fixed_input_tokens=1_600,
        label="codex:test",
    )

    assert measurement.characters == payload.characters
    assert measurement.estimated_tokens == payload.estimated_tokens + 1_600
    assert measurement.estimated_tokens >= _policy().auto_compact_tokens
    assert measurement.estimated_tokens < _policy().maximum_input_tokens


def test_preflight_rejects_estimated_tokens_at_the_hard_boundary() -> None:
    messages = [{"role": "user", "content": "request"}]
    contract = {"output_schema": {"type": "object"}}
    payload_tokens = measure_context(messages, contract).estimated_tokens
    fixed_input_tokens = 1_000
    maximum_input_tokens = payload_tokens + fixed_input_tokens
    policy = ContextWindowPolicy(
        context_window_tokens=maximum_input_tokens + 200,
        output_reserve_tokens=100,
        safety_margin_tokens=100,
        auto_compact_tokens=maximum_input_tokens,
        compact_target_tokens=maximum_input_tokens - 1,
    )

    with pytest.raises(ContextWindowOverflow) as captured:
        preflight_context(
            messages,
            contract,
            policy=policy,
            maximum_input_characters=None,
            fixed_input_tokens=fixed_input_tokens,
            label="codex:test",
        )

    message = str(captured.value)
    assert "initial context budget exceeded" in message
    assert (
        f"estimated token budget {maximum_input_tokens}>="
        f"{maximum_input_tokens}"
    ) in message
    assert f"payload={payload_tokens}" in message
    assert f"fixed_input_tokens={fixed_input_tokens}" in message


def test_preflight_character_limit_is_exclusive() -> None:
    messages = [{"role": "user", "content": "request"}]
    contract = {"tools": ["lookup"]}
    characters = measure_context(messages, contract).characters

    measurement = preflight_context(
        messages,
        contract,
        policy=None,
        maximum_input_characters=characters,
        label="claude:test",
    )

    assert measurement.characters == characters
    with pytest.raises(ContextWindowOverflow, match="serialized character budget"):
        preflight_context(
            messages,
            contract,
            policy=None,
            maximum_input_characters=characters - 1,
            label="claude:test",
        )


@pytest.mark.parametrize("fixed_input_tokens", [-1, True, 1.5])
def test_preflight_rejects_invalid_fixed_input_tokens(
    fixed_input_tokens: object,
) -> None:
    with pytest.raises(ValueError, match="fixed_input_tokens"):
        preflight_context(
            [{"role": "user", "content": "request"}],
            contract={},
            policy=None,
            maximum_input_characters=None,
            fixed_input_tokens=fixed_input_tokens,  # type: ignore[arg-type]
            label="test",
        )


def test_below_soft_limit_does_not_materialize_checkpoint() -> None:
    checkpoints = []
    controller = ContextWindowController(
        policy=_policy(),
        maximum_input_characters=None,
        label="test",
    )

    update = controller.prepare(
        [{"role": "user", "content": "short request"}],
        contract={},
        checkpoint=lambda: checkpoints.append("called") or {},
    )

    assert update.compacted is False
    assert update.window_number == 1
    assert checkpoints == []


def test_auto_compaction_replaces_old_batch_and_preserves_newest_batch() -> None:
    old_payload = "old-result-" + "x" * 5_000
    newest_payload = "newest-result-" + "y" * 600
    messages = [
        {"role": "user", "content": "answer from evidence"},
        _assistant_tool_call("old", "retrieve"),
        _tool_result("old", old_payload),
        _assistant_tool_call("new", "read_trieve_item"),
        _tool_result("new", newest_payload),
    ]
    controller = ContextWindowController(
        policy=_policy(),
        maximum_input_characters=None,
        label="test",
    )

    update = controller.prepare(
        messages,
        contract={"tools": ["retrieve", "read_trieve_item"]},
        checkpoint=lambda: {
            "draft_catalog": [{"video_chunk_id": "chunk-1"}],
        },
    )

    assert update.compacted is True
    assert update.reason == "auto"
    assert update.window_number == 2
    assert update.previous_window_id is not None
    assert [message["role"] for message in update.messages] == [
        "user",
        "user",
        "assistant",
        "tool",
    ]
    rendered = json.dumps(update.messages, ensure_ascii=False)
    assert old_payload not in rendered
    assert newest_payload in rendered
    assert "chunk-1" in update.messages[1]["content"]
    assert "navigation checkpoint, not primary evidence" in update.messages[1][
        "content"
    ]


def test_initial_snapshot_becomes_compactable_but_task_anchor_survives() -> None:
    snapshot = "historical-context-" + "x" * 5_000
    newest_payload = "newest-result-" + "y" * 600
    messages = [
        {"role": "system", "content": "fixed role and skill contract"},
        {"role": "user", "content": "stable task anchor"},
        {"role": "user", "content": snapshot},
        _assistant_tool_call("old", "retrieve"),
        _tool_result("old", "old-result"),
        _assistant_tool_call("new", "read_trieve_item"),
        _tool_result("new", newest_payload),
    ]
    controller = ContextWindowController(
        policy=_policy(),
        maximum_input_characters=None,
        label="test",
    )

    update = controller.prepare(
        messages,
        contract={"tools": ["retrieve", "read_trieve_item"]},
        checkpoint=lambda: {"retrieval_item_ids": ["chunk-1"]},
    )

    assert update.compacted is True
    rendered = json.dumps(update.messages, ensure_ascii=False)
    assert "fixed role and skill contract" in rendered
    assert "stable task anchor" in rendered
    assert snapshot not in rendered
    assert newest_payload in rendered
    assert "chunk-1" in rendered


@pytest.mark.parametrize("feedback,expected", [
    (
        "Invalid arguments for write_context_canvas: "
        "operations.1.add_event.event.SCENE.event_type_unused: Extra inputs are not permitted. "
        "Fix them and call the tool again.",
        ["event_type_unused", "Extra inputs are not permitted", "Fix them"],
    ),
    (
        json.dumps({"error": {"code": "STALE_REVISION", "message": "Expected revision 6, current is 7.",
                              "retryable": True, "retry_hint": "Read the current Canvas and retry."}}),
        ["STALE_REVISION", "current is 7", "Read the current Canvas"],
    ),
    (
        json.dumps({"batch_id": "accepted-batch", "revision": 7, "replayed": False,
                    "results": [{"event_ref": "event-1"}]}),
        ["accepted-batch", '"revision":7', '"replayed":false'],
    ),
])
def test_media_compaction_keeps_recent_write_feedback_after_evidence_reread(feedback, expected):
    # Native Kimi trace: rejected SCENE field, then reread, then the 1000-char
    # summary previously kept old reads and dropped the actual schema error.
    anchor = {"role": "user", "content": [
        {"type": "text", "text": "task " * 2600},
        *({"type": "image_url", "image_url": {"url": f"data:image/png;base64,frame-{i}"}}
          for i in range(37)),
    ]}
    messages = [anchor]
    for number in range(4):
        messages.extend([
            _assistant_tool_call(f"old-{number}", "get_context_canvas"),
            _tool_result(f"old-{number}", "old evidence " * 1000),
        ])
    messages.extend([
        _assistant_tool_call("write", "write_context_canvas"), _tool_result("write", feedback),
        _assistant_tool_call("reread", "get_context_canvas"),
        _tool_result("reread", '{"revision":7,"content":"latest evidence"}'),
    ])
    controller = ContextWindowController(
        policy=ContextWindowPolicy(128000, 8192, 2048, 90000, 60000),
        maximum_input_characters=400000, label="media-feedback",
    )
    update = controller.prepare(messages, contract={"schema": "x" * 24000}, checkpoint=lambda: {})
    assert update.compacted and update.reason == "auto"
    assert update.cleared_tool_results == 0
    assert update.messages[0] == anchor
    assert update.messages[-1] == messages[-1]
    summary = update.messages[1]["content"].split("<completed_activity>\n", 1)[1].split("\n</completed_activity>", 1)[0]
    assert len(summary) <= 1000
    assert all(value in summary for value in expected)
    assert "old evidence " * 100 not in summary
    assert update.after.estimated_tokens < controller.policy.maximum_input_tokens


def test_provider_input_usage_takes_precedence_over_local_estimate() -> None:
    controller = ContextWindowController(
        policy=_policy(),
        maximum_input_characters=None,
        label="test",
    )
    controller.record_provider_input_tokens(
        1_500,
        request_estimated_tokens=100,
    )
    messages = [
        {"role": "user", "content": "answer"},
        _assistant_tool_call("old", "retrieve"),
        _tool_result("old", "small old result"),
        _assistant_tool_call("new", "read_trieve_item"),
        _tool_result("new", "small new result"),
    ]

    update = controller.prepare(
        messages,
        contract={},
        checkpoint=lambda: {},
    )

    assert update.before.estimated_tokens >= 1_500
    assert update.compacted is True
    assert update.reason == "auto"


def test_provider_media_usage_calibrates_downward_and_keeps_fresh_evidence() -> None:
    anchor = {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,frame-{i}"}}
        for i in range(198)
    ]}
    initial = [anchor, _assistant_tool_call("read", "get_context_canvas")]
    controller = ContextWindowController(
        policy=ContextWindowPolicy(262144, 8192, 4096, 200000, 150000),
        maximum_input_characters=600000, label="karpathy-media",
    )
    estimate = measure_context(initial, {}).estimated_tokens
    assert estimate > controller.policy.maximum_input_tokens
    controller.record_provider_input_tokens(248000, request_estimated_tokens=estimate)
    evidence = _tool_result("read", '{"text":"fresh transcript that must be read"}')

    update = controller.prepare([*initial, evidence], contract={}, checkpoint=lambda: {})

    assert update.before.estimated_tokens < controller.policy.maximum_input_tokens
    assert update.cleared_tool_results == 0
    assert update.messages[-1] == evidence
    # A provider reporting a missing/zero usage must not erase the calibration.
    controller.record_provider_input_tokens(0, request_estimated_tokens=estimate)
    assert controller.prepare([*initial, evidence], contract={}).before == update.before


def test_fixed_media_prefix_above_soft_trigger_keeps_previous_read_until_hard_limit():
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,frame"}},
    ]}]
    controller = ContextWindowController(policy=_policy(), maximum_input_characters=None, label="fixed-media")
    controller.record_provider_input_tokens(2500, request_estimated_tokens=measure_context(messages, {}).estimated_tokens)
    history = [*messages,
               _assistant_tool_call("asr", "get_context_canvas"), _tool_result("asr", "complete dialogue"),
               _assistant_tool_call("member", "get_context_canvas"), _tool_result("member", "current member")]
    update = controller.prepare(history, contract={})
    assert update.before.estimated_tokens > controller.policy.auto_compact_tokens
    assert update.compacted is False
    assert update.messages == history


def test_hard_limit_releases_even_newest_tool_output_as_recoverable_receipt() -> None:
    messages = [
        {"role": "user", "content": "answer"},
        _assistant_tool_call("current", "read_draft_item"),
        _tool_result("current", "z" * 12_000),
    ]
    controller = ContextWindowController(
        policy=None,
        maximum_input_characters=2_000,
        label="test",
    )

    update = controller.prepare(
        messages,
        contract={},
        checkpoint=lambda: {"draft_catalog": ["chunk-1"]},
    )

    assert update.compacted is True
    assert update.reason == "hard_limit"
    assert update.cleared_tool_results == 1
    receipt = json.loads(update.messages[-1]["content"])
    result_summary = receipt.pop("result_summary")
    assert "feedback=" in result_summary and len(result_summary) <= 500
    assert receipt == {
        "context_window": "tool_result_released",
        "tool": "read_draft_item",
        "tool_call_id": "current",
        "original_bytes": 12_000,
        "recoverable": True,
        "instruction": (
            "Canonical state remains outside message history. Use the registered "
            "Catalog/Read Tools to inspect it. Do not repeat a mutation solely "
            "because this result was released."
        ),
    }
    assert update.after.characters <= 2_000


@pytest.mark.parametrize("payload,expected", [
    (
        "Invalid arguments for write_context_canvas: event_type_unused is forbidden. " + "details " * 1000,
        ["Invalid arguments", "event_type_unused"],
    ),
    (
        json.dumps({"error": {"code": "STALE_REVISION", "retryable": True,
                              "retry_hint": "Read revision 7 before retrying.", "message": "details " * 1000}}),
        ["STALE_REVISION", "Read revision 7"],
    ),
    (
        json.dumps({"batch_id": "accepted-batch", "revision": 7, "replayed": True,
                    "results": ["bulk-result" * 1000]}),
        ["accepted-batch", '"revision":7', '"replayed":true'],
    ),
])
def test_hard_release_keeps_bounded_tool_feedback_across_later_compaction(payload, expected):
    messages = [{"role": "user", "content": "answer"},
                _assistant_tool_call("current", "write_context_canvas"), _tool_result("current", payload)]
    controller = ContextWindowController(policy=None, maximum_input_characters=2000, label="feedback")
    update = controller.prepare(messages, contract={}, checkpoint=lambda: {})
    assert update.cleared_tool_results == 1
    receipt = json.loads(update.messages[-1]["content"])
    assert len(receipt["result_summary"]) <= 500
    assert all(value in receipt["result_summary"] for value in expected)
    assert update.after.characters <= 2000
    # Releasing a bulk payload must not lose its feedback on the next window.
    newer = [*update.messages, _assistant_tool_call("reread", "get_context_canvas"),
             _tool_result("reread", "new evidence")]
    controller = ContextWindowController(policy=_policy(), maximum_input_characters=None, label="feedback-auto")
    second = controller.prepare(newer, contract={"schema": "s" * 4000}, checkpoint=lambda: {})
    assert second.compacted
    rendered = json.dumps(second.messages, ensure_ascii=False)
    assert all(value.replace('"', '\\"') in rendered for value in expected)


@pytest.mark.parametrize(("policy", "character_threshold"), [(None, 100), (_policy(), None)])
def test_uncompactable_fixed_prefix_reaches_provider_after_compaction_warning(
    policy, character_threshold, caplog,
) -> None:
    controller = ContextWindowController(
        policy=policy,
        maximum_input_characters=character_threshold,
        label="test",
    )

    original = [{"role": "user", "content": "x" * 30_000}]
    update = controller.prepare(original, contract={}, checkpoint=lambda: {})

    assert update.messages[0] == original[0]
    assert update.compacted is True
    assert controller.state.window_number == 2
    assert controller.state.compaction_count == 1
    assert "event=agent_context_compaction_over_estimate" in caplog.text
    assert "x" * 100 not in caplog.text


def test_repeated_bounded_tool_batches_roll_across_multiple_windows() -> None:
    policy = ContextWindowPolicy(
        context_window_tokens=64_000,
        output_reserve_tokens=4_096,
        safety_margin_tokens=4_096,
        auto_compact_tokens=48_000,
        compact_target_tokens=24_000,
    )
    controller = ContextWindowController(
        policy=policy,
        maximum_input_characters=200_000,
        label="stress",
    )
    messages = [{"role": "user", "content": "question" * 500}]

    for round_number in range(1, 7):
        calls = []
        for call_number in range(4):
            call_id = f"{round_number}-{call_number}"
            calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "read_trieve_item",
                    "arguments": '{"video_chunk_id":"chunk"}',
                },
            })
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": calls,
        })
        messages.extend(
            _tool_result(call["id"], "中" * 5_000)
            for call in calls
        )
        update = controller.prepare(
            messages,
            contract={"tools": ["read_trieve_item"]},
            checkpoint=lambda: {"draft_catalog": ["chunk"]},
        )
        assert update.after.estimated_tokens <= policy.maximum_input_tokens
        assert update.after.characters <= 200_000
        if update.compacted:
            messages = update.messages

    assert controller.state.compaction_count >= 2
    assert messages[-1]["content"] == "中" * 5_000
