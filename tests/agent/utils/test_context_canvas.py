from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agent.utils.context_canvas import ContextCanvas, CanvasRevisionConflict
from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.errors import AgentToolFeedbackError, AgentToolOutputError


def _set_values(draft, operations):
    for operation in operations:
        if operation.get("fail"):
            raise AgentToolFeedbackError("INVALID_OPERATION", "Invalid operation.", "Correct the batch.")
        draft.setdefault("draft", {})[operation["reference"]] = operation["value"]
    return [{"reference": operation["reference"]} for operation in operations]


def test_host_and_snapshot_values_are_isolated() -> None:
    canvas = ContextCanvas("task-a")
    value = {"events": [{"description": "original"}]}
    canvas.put("draft", "issued", value)
    value["events"].clear()
    read = canvas.get("draft", "issued")
    read["events"][0]["description"] = "modified"
    snapshot = canvas.snapshot()
    snapshot.collections["draft"]["issued"]["events"].clear()
    assert canvas.get("draft", "issued")["events"] == [{"description": "original"}]


def test_failed_batch_exposes_no_partial_state_or_replay_record() -> None:
    canvas = ContextCanvas("task")
    operations = [{"reference": "first", "value": 1}, {"reference": "second", "value": 2, "fail": True}]
    with pytest.raises(AgentToolFeedbackError, match="Invalid operation"):
        canvas.atomic_batch("batch", 0, operations, _set_values, role="query")
    assert canvas.items("draft") == []
    assert canvas.revision == 0
    receipt = canvas.atomic_batch("batch", 0, operations[:1], _set_values, role="query")
    assert receipt["revision"] == 1
    assert canvas.items("draft") == [("first", 1)]


def test_batch_replay_is_idempotent_and_payload_reuse_is_rejected() -> None:
    canvas = ContextCanvas("task")
    operations = [{"reference": "first", "value": 1}]
    first = canvas.atomic_batch("batch", 0, operations, _set_values, role="query")
    replay = canvas.atomic_batch("batch", 0, operations, _set_values, role="query")
    assert replay == {**first, "replayed": True}
    reordered = [{"value": 1, "reference": "first"}]
    assert canvas.atomic_batch("batch", 0, reordered, _set_values, role="query") == replay
    assert canvas.revision == 1
    first["results"].clear()
    assert canvas.atomic_batch("batch", 0, operations, _set_values, role="query")["results"]
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.atomic_batch("batch", 1, [{"reference": "first", "value": 2}], _set_values, role="query")
    assert error.value.code == "BATCH_ID_REUSED"
    assert canvas.get("draft", "first") == 1


def test_replay_keys_do_not_cross_role_or_task_boundaries() -> None:
    canvas = ContextCanvas("task")
    operations = [{"reference": "first", "value": 1}]
    canvas.atomic_batch("batch", 0, operations, _set_values, role="query")
    with pytest.raises(CanvasRevisionConflict):
        canvas.atomic_batch("batch", 0, operations, _set_values, role="chat")
    other = ContextCanvas("other-task")
    assert other.atomic_batch("batch", 0, operations, _set_values, role="query")["replayed"] is False
    with pytest.raises(AgentToolFeedbackError) as error:
        other.get("draft", "issued-only-in-first-task")
    assert error.value.code == "UNKNOWN_REFERENCE"


def test_concurrent_writers_cannot_commit_the_same_input_revision() -> None:
    canvas = ContextCanvas("task")
    barrier = Barrier(2)

    def write(reference):
        barrier.wait(timeout=5)
        try:
            canvas.atomic_batch(reference, 0, [{"reference": reference, "value": 1}], _set_values, role="query")
            return "committed"
        except CanvasRevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, ("first", "second")))
    assert sorted(results) == ["committed", "conflict"]
    assert len(canvas.items("draft")) == 1
    assert canvas.revision == 1


def test_receipt_budget_is_checked_before_batch_commit() -> None:
    canvas = ContextCanvas("task")
    operations = [{"reference": "first", "value": 1}]
    with pytest.raises(AgentToolOutputError):
        canvas.atomic_batch("batch", 0, operations, _set_values, role="query", maximum_result_bytes=10)
    assert canvas.revision == 0
    assert canvas.items("draft") == []
    canvas.atomic_batch("batch", 0, operations, _set_values, role="query", maximum_result_bytes=1024)
    assert canvas.get("draft", "first") == 1


def test_evidence_registration_and_freezing_are_atomic() -> None:
    canvas = ContextCanvas("task")

    def register(draft):
        draft["evidence"] = {"first": {"text": "raw"}}
        draft["draft"] = {"first": {"text": "proposal"}}

    canvas.mutate(register, readonly_collections=("evidence",))
    assert canvas.snapshot().readonly_collections == frozenset({"evidence"})
    canvas.put("evidence", "second", {"text": "new raw evidence"})
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.put("evidence", "first", {"text": "changed"})
    assert error.value.code == "READONLY_RESOURCE"
    before = canvas.snapshot()

    def change_evidence(draft, operations):
        draft["draft"]["first"] = {"text": "changed draft"}
        draft["evidence"]["third"] = {"text": "model evidence"}
        return []

    with pytest.raises(AgentToolFeedbackError):
        canvas.atomic_batch("bad", canvas.revision, [], change_evidence, role="query")
    assert canvas.snapshot() == before


def test_readonly_single_entry_and_invalid_freeze_leave_state_intact() -> None:
    canvas = ContextCanvas("task")
    canvas.put("evidence", "first", {"text": "raw"}, readonly=True)
    before = canvas.snapshot()
    with pytest.raises(AgentToolFeedbackError):
        canvas.mutate(lambda draft: draft["evidence"].pop("first"))
    with pytest.raises(ValueError, match="Readonly targets"):
        canvas.mutate(lambda draft: draft.setdefault("draft", {}).update(first=1), readonly_collections=("missing",))
    assert canvas.snapshot() == before


class _Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["set"]
    reference: str
    value: int


class _Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str
    expected_revision: int = Field(ge=0, strict=True)
    operations: list[_Operation]


class _WritableCanvas(ContextCanvas):
    def write(self, *, role, batch_id, expected_revision, operations, maximum_result_bytes):
        normalized = [_Operation.model_validate(item).model_dump() for item in operations]
        return self.atomic_batch(batch_id, expected_revision, normalized, _set_values, role=role,
                                 maximum_result_bytes=maximum_result_bytes)


def test_shared_tools_bind_role_scope_schema_and_actual_call_budget() -> None:
    import json

    canvas = _WritableCanvas("task")
    calls = []
    checks = []
    tools = {tool.name: tool for tool in build_context_canvas_tools(
        canvas, role="query", read_collections=("draft",), write_args_schema=_Batch,
        maximum_result_bytes=1024, maximum_read_chars=100, maximum_catalog_items=1,
        check_control=lambda: checks.append(True), on_call=calls.append,
    )}
    invalid = json.loads(tools["write_context_canvas"].invoke({
        "batch_id": "batch", "expected_revision": 0, "operations": [], "scope": "other-task",
    }))
    assert invalid["error"]["code"] == "INVALID_ARGUMENTS"
    assert canvas.revision == 0
    assert calls == []
    tools["write_context_canvas"].invoke({
        "batch_id": "batch", "expected_revision": 0,
        "operations": [{"op": "set", "reference": "first", "value": 1}],
    })
    catalog = json.loads(tools["get_context_canvas"].invoke({"collection": "draft"}))
    assert catalog["items"] == [{"reference": "first"}]
    assert calls == ["write_context_canvas", "get_context_canvas"]
    assert len(checks) == 4
    denied = json.loads(tools["get_context_canvas"].invoke({"collection": "hidden"}))
    assert denied["error"]["code"] == "FORBIDDEN_COLLECTION"
    read_only = build_context_canvas_tools(
        canvas, role="evaluator", read_collections=("draft",), maximum_result_bytes=1024,
        maximum_read_chars=100, maximum_catalog_items=1,
    )
    assert [tool.name for tool in read_only] == ["get_context_canvas"]


def test_shared_reads_recover_exact_state_and_invalidate_stale_catalogs() -> None:
    import json

    canvas = ContextCanvas("task")
    canvas.put("evidence", "first", {"text": "杯" * 150}, readonly=True)
    canvas.put("evidence", "second", {"text": "tail"}, readonly=True)
    audit = []
    tool = build_context_canvas_tools(
        canvas, role="evaluator", read_collections=("evidence",), maximum_result_bytes=512,
        maximum_read_chars=100, maximum_catalog_items=1, on_read=audit.append,
    )[0]
    first = json.loads(tool.invoke({"collection": "evidence"}))
    assert first["next_cursor"]
    assert "杯" not in json.dumps(canvas.checkpoint(), ensure_ascii=False)
    overview = json.loads(tool.invoke({"collection": "evidence", "reference": "first"}))
    total = overview["sections"][0]["total_chars"]
    start, parts = 0, []
    while start < total:
        page = json.loads(tool.invoke({"collection": "evidence", "reference": "first", "section": "value",
                                      "start": start, "end": total}))
        if "error" in page:
            assert page["error"]["code"] == "RANGE_TOO_LARGE"
            page = json.loads(tool.invoke({"collection": "evidence", "reference": "first", "section": "value",
                                          "start": start, "end": page["error"]["suggested_end"]}))
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= 512
        parts.append(page["content"])
        start = page["end"]
    assert json.loads("".join(parts)) == canvas.get("evidence", "first")
    canvas.put("evidence", "third", {"text": "added"}, readonly=True)
    stale = json.loads(tool.invoke({"collection": "evidence", "cursor": first["next_cursor"]}))
    assert stale["error"]["code"] == "INVALID_CATALOG_CURSOR"
    assert all("error" not in item for item in audit)
