from __future__ import annotations

import json
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


def _batch_read_tool(canvas, **options):
    settings = dict(role="evaluator", read_collections=("evidence",), maximum_result_bytes=4096,
                    maximum_read_chars=2048, maximum_catalog_items=8)
    settings.update(options)
    return build_context_canvas_tools(canvas, **settings)[0]


def test_catalog_returns_all_entries_until_actual_bytes_require_paging() -> None:
    canvas = ContextCanvas("catalog")
    for index in range(30):
        canvas.put("evidence", f"entry-{index:02}-" + "x" * 32, 1)
    tool = _batch_read_tool(canvas, maximum_result_bytes=4096, maximum_catalog_items=1)
    full = json.loads(tool.invoke({"collection": "evidence"}))
    assert full["items"] == canvas.catalog("evidence", role="evaluator")
    assert not full.get("next_cursor")

    bounded = _batch_read_tool(canvas, maximum_result_bytes=700, maximum_catalog_items=1)
    first = json.loads(bounded.invoke({"collection": "evidence"}))
    assert first["next_cursor"] and len(first["items"]) > 1
    recovered = list(first["items"])
    page = first
    while page.get("next_cursor"):
        raw = bounded.invoke({"collection": "evidence", "catalog_cursor": page["next_cursor"]})
        assert len(raw.encode("utf-8")) <= 700
        page = json.loads(raw)
        recovered.extend(page["items"])
    assert recovered == full["items"]
    canvas.put("evidence", "new", 1)
    stale = json.loads(bounded.invoke({"collection": "evidence", "catalog_cursor": first["next_cursor"]}))
    assert stale["error"]["code"] == "INVALID_CATALOG_CURSOR"


def test_small_full_catalog_does_not_reserve_an_unneeded_cursor() -> None:
    canvas = ContextCanvas("small-catalog")
    canvas.put("evidence", "a", 1)
    canvas.put("evidence", "b", 2)
    tool = _batch_read_tool(canvas, maximum_result_bytes=200)
    raw = tool.invoke({"collection": "evidence"})
    result = json.loads(raw)
    assert len(raw.encode("utf-8")) <= 200 and result["next_cursor"] is None
    assert result["items"] == [{"reference": "a"}, {"reference": "b"}]


def test_batch_detail_reads_default_to_complete_content_without_mutation() -> None:
    canvas = ContextCanvas("batch")
    for reference in ("second", "first"):
        canvas.put("evidence", reference, {"text": reference}, readonly=True)
    before = canvas.snapshot()
    calls, audit = [], []
    tool = _batch_read_tool(canvas, on_call=calls.append, on_read=audit.append)
    references = ["first", "second"]
    page = json.loads(tool.invoke({"collection": "evidence", "references": references}))
    expected = []
    for reference in references:
        content = canvas.sections("evidence", reference, role="evaluator")["content"]
        expected.append({"collection": "evidence", "revision": canvas.revision,
                         "reference": reference, "id": reference, "section": "content",
                         "start": 0, "end": len(content), "total_chars": len(content), "content": content})
    assert page["items"] == expected
    assert (page["collection"], page["revision"], page["section"], page["total"],
            page["truncated"], page["msg"], page["remaining_references"]) == (
        "evidence", canvas.revision, "content", 2, False, "", [],
    )
    assert not {"next_cursor", "complete_end", "token_limit", "token_encoding"} & page.keys()
    assert calls == ["get_context_canvas"] and audit == expected
    assert canvas.snapshot() == before
    single = json.loads(tool.invoke({"collection": "evidence", "reference": "first"}))
    assert single == expected[0]
    overview = json.loads(tool.invoke({"collection": "evidence", "reference": "first", "section": "overview"}))
    assert overview["section"] == "overview" and overview["sections"][0]["name"] == "content"


def test_batch_remaining_references_recover_all_chinese_content_within_byte_budget() -> None:
    canvas = ContextCanvas("paged-batch")
    references = [f"entry-{index}" for index in range(6)]
    for reference in references:
        canvas.put("evidence", reference, {"text": "杯" * 60}, readonly=True)
    calls, audit = [], []
    tool = _batch_read_tool(canvas, maximum_result_bytes=1400,
                            on_call=calls.append, on_read=audit.append)
    before = canvas.snapshot()
    recovered, pending, pages = [], references, 0
    while pending:
        raw = tool.invoke({"collection": "evidence", "references": pending})
        page = json.loads(raw)
        assert "error" not in page and page["items"]
        assert len(raw.encode("utf-8")) <= 1400
        assert page["total"] == len(pending) and page["revision"] == before.revision
        complete = page["items"]
        assert all(not item.get("truncated", False) and "content" in item for item in complete)
        assert [item["reference"] for item in complete] + page["remaining_references"] == pending
        assert page["truncated"] == bool(page["remaining_references"])
        assert bool(page["msg"]) == page["truncated"]
        assert "complete_end" not in page and "next_cursor" not in page
        recovered.extend(complete)
        pending = page["remaining_references"]
        pages += 1
        assert pages <= len(references) and audit == recovered
    assert pages > 1 and [item["reference"] for item in recovered] == references
    assert [json.loads(item["content"]) for item in recovered] == [canvas.get("evidence", ref) for ref in references]
    assert calls == ["get_context_canvas"] * pages and canvas.snapshot() == before


def test_large_first_item_preserves_unread_order_and_can_be_read_by_character_ranges() -> None:
    canvas = ContextCanvas("large-batch")
    canvas.put("evidence", "large", {"text": "杯" * 500}, readonly=True)
    canvas.put("evidence", "small", 1, readonly=True)
    before, audit = canvas.snapshot(), []
    tool = _batch_read_tool(canvas, maximum_result_bytes=700, maximum_read_chars=1,
                            maximum_catalog_items=1, on_read=audit.append)
    raw = tool.invoke({"collection": "evidence", "references": ["large", "small"]})
    result = json.loads(raw)
    assert len(raw.encode("utf-8")) <= 700
    assert result["truncated"] and result["remaining_references"] == ["large", "small"]
    assert result["items"] == [] and audit == []
    assert result["msg"]
    small = json.loads(tool.invoke({"collection": "evidence", "references": ["small"]}))
    assert not small["truncated"] and small["remaining_references"] == []
    assert [item["reference"] for item in small["items"]] == ["small"]
    assert small["items"][0]["content"] == "1" and audit == small["items"]
    overview = json.loads(tool.invoke({"collection": "evidence", "reference": "large", "section": "overview"}))
    total = overview["sections"][0]["total_chars"]
    audit.clear()
    start, parts = 0, []
    while start < total:
        request = {"collection": "evidence", "reference": "large", "char_start": start, "char_end": total}
        raw = tool.invoke(request)
        page = json.loads(raw)
        if "error" in page:
            assert page["error"]["code"] == "RANGE_TOO_LARGE"
            suggested_end = page["error"]["suggested_end"]
            assert page["error"]["retry_hint"] == (
                f"Retry the same section with char_start={start} and char_end={suggested_end}; "
                "then continue with char_start set to the returned end."
            )
            request["char_end"] = suggested_end
            raw = tool.invoke(request)
            page = json.loads(raw)
        assert len(raw.encode("utf-8")) <= 700 and "error" not in page
        assert page["start"] == start and page["end"] - start == len(page["content"])
        assert page["end"] > start + 1
        parts.append(page["content"])
        start = page["end"]
    assert json.loads("".join(parts)) == canvas.get("evidence", "large")
    assert [item["content"] for item in audit] == parts
    assert canvas.snapshot() == before


def test_legacy_caps_do_not_limit_reference_count_characters_or_model_tokens() -> None:
    canvas = ContextCanvas("legacy-caps")
    references = [f"entry-{index}" for index in range(12)]
    for index, reference in enumerate(references):
        canvas.put("evidence", reference, {"text": "a " * 9000 + "<|endoftext|>" if index == 0 else reference})
    audit = []
    tool = _batch_read_tool(canvas, maximum_result_bytes=64_000,
                            maximum_read_chars=1, maximum_catalog_items=1, on_read=audit.append)
    raw = tool.invoke({"collection": "evidence", "references": references})
    result = json.loads(raw)
    assert len(raw.encode("utf-8")) <= 64_000
    assert not result["truncated"] and result["remaining_references"] == []
    assert [item["reference"] for item in result["items"]] == references
    assert [json.loads(item["content"]) for item in result["items"]] == [canvas.get("evidence", ref) for ref in references]
    assert audit == result["items"]


def test_small_complete_batch_does_not_reserve_an_unneeded_truncation_warning() -> None:
    canvas = ContextCanvas("small-complete-batch")
    canvas.put("evidence", "first", 1)
    tool = _batch_read_tool(canvas, maximum_result_bytes=350)
    raw = tool.invoke({"collection": "evidence", "references": ["first"]})
    result = json.loads(raw)
    assert len(raw.encode("utf-8")) <= 350
    assert not result["truncated"] and result["remaining_references"] == [] and result["msg"] == ""
    assert result["items"][0]["content"] == "1"


@pytest.mark.parametrize("references", [[], "first", [1], [""], ["first", None]])
def test_batch_reference_schema_rejects_empty_and_malformed_lists_before_call(references) -> None:
    canvas = ContextCanvas("schema")
    calls, audit = [], []
    tool = _batch_read_tool(canvas, on_call=calls.append, on_read=audit.append)
    result = json.loads(tool.invoke({"collection": "evidence", "references": references}))
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    assert calls == [] and audit == [] and canvas.revision == 0


@pytest.mark.parametrize("overrides", [
    {"reference": "first"}, {"section": "overview"}, {"catalog_cursor": "not-a-cursor"},
    {"references": ["first", "first"]}, {"char_start": 1}, {"char_end": 1},
])
def test_invalid_batch_read_combinations_return_feedback_without_read_audit(overrides) -> None:
    canvas = ContextCanvas("invalid-batch")
    canvas.put("evidence", "first", 1)
    before, audit = canvas.snapshot(), []
    tool = _batch_read_tool(canvas, on_read=audit.append)
    result = json.loads(tool.invoke({"collection": "evidence", "references": ["first"], **overrides}))
    assert result["error"]["code"] == "INVALID_READ"
    assert audit == [] and canvas.snapshot() == before


@pytest.mark.parametrize("overrides", [{"cursor": "legacy"}, {"start": 0}, {"end": 1}])
@pytest.mark.parametrize("target", [{}, {"reference": "first"}, {"references": ["first"]}])
def test_legacy_read_arguments_are_rejected_before_call(overrides, target) -> None:
    canvas = ContextCanvas("legacy-arguments")
    canvas.put("evidence", "first", 1)
    calls, audit = [], []
    tool = _batch_read_tool(canvas, on_call=calls.append, on_read=audit.append)
    result = json.loads(tool.invoke({"collection": "evidence", **target, **overrides}))
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    assert calls == [] and audit == []


@pytest.mark.parametrize("overrides,code", [
    ({"references": ["first", "missing"]}, "UNKNOWN_REFERENCE"),
    ({"section": "missing"}, "UNKNOWN_SECTION"),
    ({"collection": "hidden"}, "FORBIDDEN_COLLECTION"),
])
def test_batch_scope_and_lookup_errors_expose_no_partial_reads(overrides, code) -> None:
    canvas = ContextCanvas("lookup-batch")
    canvas.put("evidence", "first", {"text": "original"}, readonly=True)
    before, audit = canvas.snapshot(), []
    tool = _batch_read_tool(canvas, on_read=audit.append)
    result = json.loads(tool.invoke({"collection": "evidence", "references": ["first"], **overrides}))
    assert result["error"]["code"] == code
    assert audit == [] and canvas.snapshot() == before


def test_batch_metadata_over_byte_budget_requests_fewer_references_without_audit() -> None:
    canvas = ContextCanvas("metadata-batch")
    references = [f"{index}-" + "😃" * 128 for index in range(8)]
    for reference in references:
        canvas.put("evidence", reference, 1)
    before, audit = canvas.snapshot(), []
    tool = _batch_read_tool(canvas, maximum_result_bytes=512, on_read=audit.append)
    result = json.loads(tool.invoke({"collection": "evidence", "references": references}))
    assert result["error"]["code"] == "BATCH_TOO_LARGE"
    assert "references" in result["error"]["retry_hint"]
    assert audit == [] and canvas.snapshot() == before
