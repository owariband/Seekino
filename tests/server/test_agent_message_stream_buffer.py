from __future__ import annotations

from server.model.dto.agentDTO import AgentMessageSendState, AgentMessageState
from server.service.agent_chat import AgentMessageExecution


def _message(message_id: str, role: str) -> AgentMessageState:
    return AgentMessageState(
        message_id=message_id,
        conversation_id="conversation-1",
        sequence_no=1,
        role=role,
        status="PROCESSING",
        content=None,
        series_ids=(),
        model_name=None,
        search_result=None,
        last_error=None,
        created_at=0,
        updated_at=0,
    )


def _execution() -> AgentMessageExecution:
    return AgentMessageExecution(
        AgentMessageSendState(
            _message("user-1", "USER"),
            _message("assistant-1", "ASSISTANT"),
        ),
        stream=True,
    )


def _read_events(execution: AgentMessageExecution) -> list[dict | None]:
    events = []
    while True:
        event = execution.next_event(timeout=1)
        events.append(event)
        if event is None:
            return events


def test_backlogged_snapshots_keep_only_the_newest_unsent_one():
    execution = _execution()
    buffered = 0
    for index in range(1000):
        execution.publish({"type": "answer.snapshot", "content": f"answer-{index}"})
        # The trailing None is the end marker, not a buffered event.
        buffered = max(buffered, sum(event is not None for event in execution._events))
    execution.publish({"type": "answer.completed"})
    buffered = max(buffered, sum(event is not None for event in execution._events))
    execution.finish()

    events = _read_events(execution)
    assert buffered <= 2
    assert [event["type"] for event in events[:-1]] == ["answer.snapshot", "answer.completed"]
    assert events[0]["content"] == "answer-999"
    assert events[0]["sequence"] == 1000
    assert events[-1] is None


def test_snapshot_coalescing_only_replaces_the_buffer_tail():
    execution = _execution()
    execution.publish({"type": "answer.snapshot", "content": "first"})
    execution.publish({"type": "answer.evidence", "searchResult": {}})
    execution.publish({"type": "answer.snapshot", "content": "second"})
    execution.publish({"type": "answer.snapshot", "content": "third"})
    execution.finish()

    events = _read_events(execution)
    assert [event["type"] for event in events[:-1]] == [
        "answer.snapshot", "answer.evidence", "answer.snapshot",
    ]
    assert events[0]["content"] == "first"
    assert events[1]["sequence"] == 2
    assert events[2]["content"] == "third"
    assert events[2]["sequence"] == 4
    assert events[-1] is None


def test_snapshot_already_taken_by_the_consumer_is_not_replaced():
    execution = _execution()
    execution.publish({"type": "answer.snapshot", "content": "first"})
    first = execution.next_event(timeout=1)
    assert first["content"] == "first"
    execution.publish({"type": "answer.snapshot", "content": "second"})
    execution.finish()

    events = _read_events(execution)
    assert first["content"] == "first"
    assert [event["content"] for event in events[:-1]] == ["second"]
    assert events[-1] is None


def test_adjacent_same_call_deltas_merge_but_new_block_or_call_breaks_them():
    execution = _execution()
    execution.publish({"type": "answer.delta", "callId": "call-1", "delta": "第", "newBlock": True, "textStream": {"deltaSequence": 1}})
    execution.publish({"type": "answer.delta", "callId": "call-1", "delta": "一", "newBlock": False, "textStream": {"deltaSequence": 2}})
    execution.publish({"type": "answer.delta", "callId": "call-1", "delta": "段", "newBlock": False, "textStream": {"deltaSequence": 3}})
    execution.publish({"type": "answer.delta", "callId": "call-1", "delta": "第二段", "newBlock": True, "textStream": {"deltaSequence": 4}})
    execution.publish({"type": "answer.delta", "callId": "call-2", "delta": "新调用", "newBlock": False, "textStream": {"deltaSequence": 5}})
    execution.finish()

    events = _read_events(execution)
    deltas = [event for event in events[:-1] if event["type"] == "answer.delta"]
    assert [event["delta"] for event in deltas] == ["第一段", "第二段", "新调用"]
    assert [event["newBlock"] for event in deltas] == [True, True, False]
    assert [event["textStream"]["deltaSequence"] for event in deltas] == [3, 4, 5]
    assert events[-1] is None


def test_adjacent_same_call_reasoning_deltas_merge():
    execution = _execution()
    execution.publish({"type": "answer.reasoning.delta", "callId": "call-1", "delta": "先想"})
    execution.publish({"type": "answer.reasoning.delta", "callId": "call-1", "delta": "一想"})
    execution.publish({"type": "answer.reasoning.delta", "callId": "call-2", "delta": "换一轮"})
    execution.finish()

    events = _read_events(execution)
    deltas = [event for event in events[:-1] if event["type"] == "answer.reasoning.delta"]
    assert [event["delta"] for event in deltas] == ["先想一想", "换一轮"]
    assert [event["callId"] for event in deltas] == ["call-1", "call-2"]
    assert events[-1] is None


def test_slow_consumer_still_interrupts_when_non_snapshot_events_fill_the_buffer():
    execution = _execution()
    for _ in range(300):
        execution.publish({"type": "answer.progress", "phase": "RESPONDING"})
    execution.finish()

    events = _read_events(execution)
    assert [event and event["type"] for event in events] == ["answer.interrupted", None]
    assert events[0]["sequence"] == 257
    assert events[0]["error"] == {
        "code": "STREAM_INTERRUPTED",
        "message": "连接未能及时接收回答，请通过会话快照恢复。",
    }
