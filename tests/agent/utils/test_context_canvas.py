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


def _batch_read_tool(canvas, **options):
    settings = dict(role="evaluator", read_collections=("evidence",), maximum_result_bytes=4096,
                    maximum_read_chars=2048, maximum_catalog_items=8)
    settings.update(options)
    return build_context_canvas_tools(canvas, **settings)[0]


def test_batch_detail_reads_return_exact_entries_in_one_call_without_mutation() -> None:
    canvas = ContextCanvas("batch")
    for reference in ("second", "first"):
        canvas.put("evidence", reference, {"text": reference}, readonly=True)
    before = canvas.snapshot()
    calls, audit = [], []
    tool = _batch_read_tool(canvas, on_call=calls.append, on_read=audit.append)
    references = ["first", "second"]
    page = json.loads(tool.invoke({"collection": "evidence", "references": references, "section": "value"}))
    expected = []
    for reference in references:
        content = canvas.sections("evidence", reference, role="evaluator")["value"]
        expected.append({"collection": "evidence", "revision": canvas.revision,
                         "reference": reference, "id": reference, "section": "value",
                         "start": 0, "end": len(content), "total_chars": len(content), "content": content})
    assert page["items"] == expected
    assert (page["collection"], page["revision"], page["section"], page["start"], page["end"],
            page["total"], page["complete_end"], page["truncated"], page["msg"]) == (
        "evidence", canvas.revision, "value", 0, 2, 2, 2, False, "",
    )
    assert "next_cursor" not in page and page["token_limit"] == 8000
    assert calls == ["get_context_canvas"] and audit == expected
    assert canvas.snapshot() == before
    catalog = json.loads(tool.invoke({"collection": "evidence"}))
    assert catalog["items"] == [{"reference": "second"}, {"reference": "first"}]
    overview = json.loads(tool.invoke({"collection": "evidence", "reference": "first"}))
    assert overview["section"] == "overview" and overview["sections"][0]["name"] == "value"
    single = json.loads(tool.invoke({"collection": "evidence", "reference": "first", "section": "value"}))
    assert single == expected[0]


@pytest.mark.parametrize("maximum_bytes,maximum_chars", [(1400, 4096), (8192, 90)], ids=["utf8", "characters"])
def test_batch_ranges_trim_later_details_and_recover_every_entry(maximum_bytes, maximum_chars) -> None:
    canvas = ContextCanvas("paged-batch")
    references = [f"entry-{index}" for index in range(6)]
    for reference in references:
        canvas.put("evidence", reference, {"text": "杯" * 60}, readonly=True)
    calls, audit = [], []
    tool = _batch_read_tool(canvas, maximum_result_bytes=maximum_bytes, maximum_read_chars=maximum_chars,
                            on_call=calls.append, on_read=audit.append)
    before = canvas.snapshot()
    request = {"collection": "evidence", "references": references, "section": "value"}
    recovered, pages, start = [], 0, 0
    for _ in range(len(references) + 1):
        raw = tool.invoke({**request, "start": start, "end": len(references)})
        page = json.loads(raw)
        assert "error" not in page and page["items"]
        assert len(raw.encode("utf-8")) <= maximum_bytes
        complete = [item for item in page["items"] if not item.get("truncated", False)]
        assert complete and sum(len(item["content"]) for item in complete) <= maximum_chars
        assert page["total"] == len(references) and page["revision"] == before.revision
        assert page["start"] == start and page["end"] == len(references)
        assert page["complete_end"] == start + len(complete)
        assert [item["reference"] for item in page["items"]] == references[start:]
        assert page["items"][:len(complete)] == complete
        for item in page["items"][len(complete):]:
            assert set(item) == {"reference", "section", "total_chars", "truncated"}
            assert item["truncated"] is True and "content" not in item
        recovered.extend(complete)
        pages += 1
        start = page["complete_end"]
        assert audit == recovered
        if not page["truncated"]:
            assert start == len(references) and page["msg"] == ""
            break
        assert "请减少item检索数目" in page["msg"] and "无法看到全量信息" in page["msg"]
        assert "next_cursor" not in page
    else:
        pytest.fail("Numeric batch ranges did not recover all details")
    assert pages > 1 and [item["reference"] for item in recovered] == references
    assert [json.loads(item["content"]) for item in recovered] == [canvas.get("evidence", ref) for ref in references]
    assert calls == ["get_context_canvas"] * pages and audit == recovered
    assert canvas.snapshot() == before


def test_batch_range_is_zero_based_end_exclusive_and_only_resolves_selected_refs() -> None:
    canvas = ContextCanvas("selected-batch")
    for reference in ("first", "second"):
        canvas.put("evidence", reference, {"text": reference}, readonly=True)
    audit = []
    tool = _batch_read_tool(canvas, on_read=audit.append)
    references = ["unselected-missing-before", "first", "second", "unselected-missing-after"]
    page = json.loads(tool.invoke({"collection": "evidence", "references": references,
                                   "section": "value", "start": 1, "end": 3}))
    assert [item["reference"] for item in page["items"]] == references[1:3]
    assert (page["start"], page["end"], page["total"], page["complete_end"]) == (1, 3, 4, 3)
    assert audit == page["items"] and not page["truncated"]
    tail = json.loads(tool.invoke({"collection": "evidence", "references": references[:3],
                                   "section": "value", "start": 2}))
    assert [item["reference"] for item in tail["items"]] == ["second"] and tail["end"] == 3


def test_small_complete_batch_does_not_reserve_an_unneeded_truncation_warning() -> None:
    canvas = ContextCanvas("small-complete-batch")
    canvas.put("evidence", "first", 1)
    tool = _batch_read_tool(canvas, maximum_result_bytes=350)
    raw = tool.invoke({"collection": "evidence", "references": ["first"], "section": "value"})
    result = json.loads(raw)
    assert len(raw.encode("utf-8")) <= 350
    assert not result["truncated"] and result["complete_end"] == 1 and result["msg"] == ""
    assert result["items"][0]["content"] == "1"


@pytest.mark.parametrize("references", [[], "first", [1], [""], ["first", None]])
def test_batch_reference_schema_rejects_empty_and_malformed_lists_before_call(references) -> None:
    canvas = ContextCanvas("schema")
    calls, audit = [], []
    tool = _batch_read_tool(canvas, on_call=calls.append, on_read=audit.append)
    result = json.loads(tool.invoke({"collection": "evidence", "references": references, "section": "value"}))
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    assert calls == [] and audit == [] and canvas.revision == 0


@pytest.mark.parametrize("overrides", [
    {"reference": "first"}, {"section": "overview"}, {"cursor": "not-a-cursor"},
    {"references": ["first", "first"]}, {"references": ["first", "second", "third"]},
])
def test_invalid_batch_read_combinations_return_feedback_without_read_audit(overrides) -> None:
    canvas = ContextCanvas("invalid-batch")
    canvas.put("evidence", "first", 1)
    before, audit = canvas.snapshot(), []
    tool = _batch_read_tool(canvas, maximum_catalog_items=2, on_read=audit.append)
    request = {"collection": "evidence", "references": ["first"], "section": "value", **overrides}
    result = json.loads(tool.invoke(request))
    assert result["error"]["code"] == "INVALID_READ"
    assert audit == [] and canvas.snapshot() == before


@pytest.mark.parametrize("overrides,code", [
    ({"start": 1}, "INVALID_RANGE"), ({"end": 0}, "INVALID_RANGE"),
    ({"end": 2}, "INVALID_RANGE"), ({"start": -1}, "INVALID_ARGUMENTS"),
    ({"start": True}, "INVALID_ARGUMENTS"), ({"end": 0.5}, "INVALID_ARGUMENTS"),
])
def test_invalid_batch_item_ranges_return_feedback_without_audit(overrides, code) -> None:
    canvas = ContextCanvas("invalid-range")
    canvas.put("evidence", "first", 1)
    before, audit = canvas.snapshot(), []
    tool = _batch_read_tool(canvas, on_read=audit.append)
    result = json.loads(tool.invoke({"collection": "evidence", "references": ["first"],
                                     "section": "value", **overrides}))
    assert result["error"]["code"] == code
    assert audit == [] and canvas.snapshot() == before


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
    request = {"collection": "evidence", "references": ["first"], "section": "value", **overrides}
    result = json.loads(tool.invoke(request))
    assert result["error"]["code"] == code
    assert audit == [] and canvas.snapshot() == before


@pytest.mark.parametrize("maximum_bytes,maximum_chars", [(4096, 10), (700, 4096)], ids=["characters", "utf8"])
def test_oversized_batch_item_trims_itself_and_later_small_items_without_audit(maximum_bytes, maximum_chars) -> None:
    canvas = ContextCanvas("large-batch")
    canvas.put("evidence", "large", {"text": "杯" * 500}, readonly=True)
    canvas.put("evidence", "small", 1, readonly=True)
    before, audit = canvas.snapshot(), []
    tool = _batch_read_tool(canvas, maximum_result_bytes=maximum_bytes, maximum_read_chars=maximum_chars,
                            on_read=audit.append)
    raw = tool.invoke({"collection": "evidence", "references": ["large", "small"], "section": "value"})
    result = json.loads(raw)
    assert len(raw.encode("utf-8")) <= maximum_bytes
    assert result["truncated"] and result["complete_end"] == 0
    assert all(item["truncated"] and "content" not in item for item in result["items"])
    assert "超限已被裁剪" in result["msg"] and "reference/start/end" in result["msg"]
    assert audit == [] and canvas.snapshot() == before
    small = json.loads(tool.invoke({"collection": "evidence", "references": ["large", "small"],
                                    "section": "value", "start": 1, "end": 2}))
    assert not small["truncated"] and small["complete_end"] == 2
    assert audit == small["items"] and canvas.snapshot() == before


@pytest.mark.parametrize("budget", ["bytes", "tokens"])
def test_batch_metadata_over_budget_returns_reduce_range_feedback_without_audit(budget) -> None:
    canvas = ContextCanvas("metadata-batch")
    references = [f"{index}-" + "😃" * 128 for index in range(100 if budget == "tokens" else 8)]
    for reference in references:
        canvas.put("evidence", reference, 1)
    before, audit = canvas.snapshot(), []
    tool = _batch_read_tool(canvas, maximum_catalog_items=100, maximum_read_chars=60_000,
                            maximum_result_bytes=64_000 if budget == "tokens" else 512, on_read=audit.append)
    result = json.loads(tool.invoke({"collection": "evidence", "references": references, "section": "value"}))
    assert result["error"]["code"] == "BATCH_RANGE_TOO_LARGE"
    assert "start/end" in result["error"]["retry_hint"]
    assert audit == [] and canvas.snapshot() == before


def test_batch_token_budget_counts_the_whole_actual_json_receipt() -> None:
    import tiktoken

    encoding = tiktoken.get_encoding("o200k_base")
    canvas = ContextCanvas("whole-json-token-budget")
    canvas.put("evidence", "first", {"text": "a " * 7940}, readonly=True)
    content = canvas.sections("evidence", "first", role="evaluator")["value"]
    assert len(encoding.encode(content, disallowed_special=())) < 8000
    audit = []
    tool = _batch_read_tool(canvas, maximum_result_bytes=64_000, maximum_read_chars=60_000,
                            on_read=audit.append)
    raw = tool.invoke({"collection": "evidence", "references": ["first"], "section": "value"})
    result = json.loads(raw)
    assert len(encoding.encode(raw, disallowed_special=())) <= 8000
    assert len(raw.encode("utf-8")) <= 64_000 and result["token_encoding"] == "o200k_base"
    assert result["truncated"] and result["complete_end"] == 0 and audit == []
    assert result["items"] == [{"reference": "first", "section": "value",
                                "total_chars": len(content), "truncated": True}]


def test_batch_tokens_trim_later_items_and_special_strings_are_plain_business_text() -> None:
    import tiktoken

    canvas = ContextCanvas("tokens-batch")
    values = ["a " * 4200 + "<|endoftext|>", "天气晴朗 " * 1400 + "<|fim_prefix|>"]
    references = ["first", "second"]
    for reference, text in zip(references, values):
        canvas.put("evidence", reference, {"text": text}, readonly=True)
    encoding = tiktoken.get_encoding("o200k_base")
    contents = [canvas.sections("evidence", ref, role="evaluator")["value"] for ref in references]
    assert sum(len(content) for content in contents) < 60_000
    assert sum(len(content.encode("utf-8")) for content in contents) < 60_000
    assert sum(len(encoding.encode(content, disallowed_special=())) for content in contents) > 8000
    audit = []
    tool = _batch_read_tool(canvas, maximum_result_bytes=64_000, maximum_read_chars=60_000,
                            on_read=audit.append)
    request = {"collection": "evidence", "references": references, "section": "value"}
    raw = tool.invoke(request)
    result = json.loads(raw)
    assert "error" not in result and len(encoding.encode(raw, disallowed_special=())) <= 8000
    assert result["truncated"] and result["complete_end"] == 1
    assert json.loads(result["items"][0]["content"])["text"] == values[0]
    assert result["items"][1]["truncated"] and "content" not in result["items"][1]
    assert audit == result["items"][:1]
    remaining = json.loads(tool.invoke({**request, "start": 1, "end": 2}))
    assert not remaining["truncated"] and remaining["complete_end"] == 2
    assert json.loads(remaining["items"][0]["content"])["text"] == values[1]
    assert [item["reference"] for item in audit] == references
