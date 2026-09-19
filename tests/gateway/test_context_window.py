import json

import pytest

from gateway.context_window import (
    ContextWindowController,
    ContextWindowOverflow,
    ContextWindowPolicy,
)


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


def test_provider_input_usage_takes_precedence_over_local_estimate() -> None:
    controller = ContextWindowController(
        policy=_policy(),
        maximum_input_characters=None,
        label="test",
    )
    controller.record_provider_input_tokens(
        1_600,
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


def test_uncompactable_fixed_prefix_fails_without_advancing_window() -> None:
    controller = ContextWindowController(
        policy=None,
        maximum_input_characters=100,
        label="test",
    )

    with pytest.raises(ContextWindowOverflow, match="message character budget"):
        controller.prepare(
            [{"role": "user", "content": "x" * 1_000}],
            contract={},
            checkpoint=lambda: {},
        )

    assert controller.state.window_number == 1
    assert controller.state.compaction_count == 0


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
