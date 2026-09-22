from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from queue import Empty
from threading import Event
from uuid import uuid4

import pytest
from langchain_core.tools import BaseTool
from sqlalchemy import event, func, select, text

from server.agent.context import _project_stored_search_result
from server.agent.errors import AgentContextBudgetError, AgentModelInvocationError
from server.agent.runtime import AgentRuntime
from server.agent.supervisor import SupervisorRuntime
from gateway.web_search import WebSearchHit, WebSearcher
from server.api.agent import _agent_message_view
from server.config.agent import AgentServiceConfig
from server.database import (
    DatabaseEnvironment, create_database_engine, create_schema, create_session_factory,
)
from server.model.agent import (
    AgentConversationRecord,
    AgentMessageRecord,
    AgentMessageTimingRecord,
)
from server.model.video import SeriesRecord
from server.repository.agent import (
    AgentConflictError, AgentConversationNotFoundError, AgentMessageInProgressError,
    AgentRepository, AgentReferenceInvalidError, AgentMessageNotFoundError,
)
from server.repository.library import LibraryRepository
from server.repository.video import VideoRepository
from server.schema import (
    AgentReply, SelectionReason, VideoChunkCandidate, VideoChunkSearchHit, VideoChunkSearchResult,
)
from server.service.agent_chat import AgentChatService, _validated_stored_video_range
from server.service.library import LibraryService


@dataclass
class _RetrievalStub:
    calls: list[tuple[str, list[str], int, str]] = field(default_factory=list)
    hits: list[VideoChunkSearchHit] = field(default_factory=list)

    def search(
        self,
        query,
        *,
        workspace_id,
        series_ids,
        top_k,
        transcript_weight=None,
        text_weight=None,
        vision_weight=None,
    ):
        if any(
            weight is not None
            for weight in (transcript_weight, text_weight, vision_weight)
        ):
            assert None not in (transcript_weight, text_weight, vision_weight)
            assert transcript_weight + text_weight + vision_weight == pytest.approx(1.0)
        self.calls.append((query, list(series_ids), top_k, workspace_id))
        return VideoChunkSearchResult(
            query=query, series_ids=list(series_ids),
            agent_reply=AgentReply(message="底层检索完成。", result_count=len(self.hits)), hits=self.hits,
        )


def _read_all(tool, video_chunk_id: str) -> dict:
    overview = json.loads(tool.invoke({
        "video_chunk_id": video_chunk_id,
        "section": "overview",
    }))
    sections = {}
    for item in overview["sections"]:
        name, total = item["name"], item["total_chars"]
        start = 0
        content = []
        while start < total:
            end = total
            while True:
                page = json.loads(tool.invoke({
                    "video_chunk_id": video_chunk_id,
                    "section": name,
                    "start": start,
                    "end": end,
                }))
                if page.get("ok") is not False:
                    break
                assert page["error"]["code"] == "RANGE_TOO_LARGE"
                end = page["error"]["suggested_end"]
                assert start < end < total
            assert page["start"] == start
            assert page["end"] == end
            content.append(page["content"])
            start = end
        sections[name] = json.loads("".join(content))
    return sections


@dataclass
class _QueryModel:
    model_name: str = "query-test"
    queries: tuple[str | None, ...] = ("第一次改写",)
    prompts: list[str] = field(default_factory=list)
    selected_ranges: tuple[tuple[str, int, int], ...] = ()

    def run_structured(self, prompt, *, tools, result_type, max_model_calls, **_kwargs):
        self.prompts.append(prompt)
        number = json.loads(prompt.rsplit("\n\n", 1)[1])["turn_number"]
        query = self.queries[min(number - 1, len(self.queries) - 1)]
        tools_by_name = {tool.name: tool for tool in tools}
        if query is not None:
            tools_by_name["retrieve"].invoke({
                "query": query,
                "top_k": 8,
                "channel_weights": {
                    "transcript": 0.1,
                    "semantic": 0.5,
                    "visual": 0.4,
                },
            })
        for video_chunk_id, start_ms, end_ms in self.selected_ranges:
            _read_all(tools_by_name["read_trieve_item"], video_chunk_id)
            tools_by_name["update_draft_item"].invoke({
                "video_chunk_id": video_chunk_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "claim": f"片段 {video_chunk_id} 支持回答。",
            })
        return result_type.model_validate({
            "plan_context": f"第 {number} 轮检索并核验动作。",
            "message": f"第 {number} 轮待审核草稿。",
        })


@dataclass
class _EvaluatorModel:
    model_name: str = "evaluator-test"
    approve_after: int = 1
    calls: int = 0

    def run_structured(self, _prompt, *, tools, result_type, **_kwargs):
        tools_by_name = {tool.name: tool for tool in tools}
        cursor = None
        item_ids = []
        while True:
            page = json.loads(tools_by_name["get_draft_catelog"].invoke({
                "cursor": cursor,
            }))
            item_ids.extend(item["video_chunk_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        for video_chunk_id in item_ids:
            _read_all(tools_by_name["read_draft_item"], video_chunk_id)
        self.calls += 1
        return result_type.model_validate({
            "action": "complete" if self.calls >= self.approve_after else "continue",
            "advice": "补充摔杯子的动作，重新核验。",
        })


@dataclass
class _ChatModel:
    model_name: str = "chat-test"
    prompts: list[str] = field(default_factory=list)
    submitted_items: list[dict] = field(default_factory=list)
    release: Event | None = None
    fail: bool = False

    def run_agent_loop(
        self, prompt, *, tools: Sequence[BaseTool], max_model_calls, handle_text,
        require_tool_call=False,
        request_timeout_seconds=None, check_control=None, max_input_chars=None,
    ):
        update_video_item = next(tool for tool in tools if tool.name == "update_video_item")
        assert set(update_video_item.args_schema.model_fields) == {"video_chunk_id", "claim"}
        self.prompts.append(prompt)
        assert request_timeout_seconds is not None
        assert request_timeout_seconds > 0
        assert max_model_calls is None or (type(max_model_calls) is int and max_model_calls > 0)
        assert check_control is not None
        if self.release is not None:
            assert self.release.wait(5), "test did not release provider"
        if self.fail:
            raise RuntimeError("private upstream failure")
        context = json.loads(prompt.rsplit("\n\n", 1)[1])
        approved_turn = context["approved_turn"]
        chunk_ids = approved_turn["video_chunk_ids"]
        assert require_tool_call is bool(chunk_ids)
        check_control()
        handle_text(
            "目前没有足够的候选画面，"
            if chunk_ids else "目前没有足够的候选画面，建议补充人物或动作。"
        )
        for current_count, chunk_id in enumerate(chunk_ids, 1):
            item = {
                "video_chunk_id": chunk_id,
                "claim": f"片段 {chunk_id} 支持回答。",
            }
            self.submitted_items.append(item)
            assert json.loads(update_video_item.invoke(item)) == {
                "accepted": True,
                "operation": "inserted",
                "video_chunk_id": chunk_id,
                "video_item_count": current_count,
            }
        if chunk_ids:
            handle_text("建议补充人物或动作。")


@pytest.fixture
def build_service(tmp_path):
    resources = []

    def build(
        *, query=None, evaluator=None, chat=None, runtime_kwargs=None,
        service_kwargs=None, enforce_foreign_keys=False, supervisor_model=None,
        web_searcher=None,
    ):
        engine = create_database_engine(
            f"sqlite+pysqlite:///{tmp_path / f'agent-{len(resources)}.sqlite'}",
            environment=DatabaseEnvironment.TEST,
        )
        if enforce_foreign_keys:
            event.listen(engine, "connect", _enable_sqlite_foreign_keys)
        create_schema(engine)
        factory = create_session_factory(engine)
        repository = AgentRepository(factory)
        library = LibraryService(LibraryRepository(factory))
        retrieval = _RetrievalStub()
        query, evaluator, chat = query or _QueryModel(), evaluator or _EvaluatorModel(), chat or _ChatModel()
        video_runtime = AgentRuntime(
            query, evaluator, chat, retrieval.search, **(runtime_kwargs or {}),
        )
        runtime = (
            SupervisorRuntime(
                video_runtime, supervisor_model, web_searcher or WebSearcher(),
                max_web_results=5, web_timeout_seconds=12,
                execution_timeout_seconds=60, max_tool_result_bytes=16_384,
                max_prompt_chars=200_000,
            )
            if supervisor_model is not None else video_runtime
        )
        service = AgentChatService(
            repository, library, runtime, **(service_kwargs or {}),
        )
        resources.append((service, engine))
        series = library.create_series(workspace_id="local-default", name="Test Series")
        with factory.begin() as session:
            session.get(SeriesRecord, series.series_id).status = "READY"
        conversation = service.create_conversation(
            workspace_id="local-default", title="测试对话",
            idempotency_key="conversation",
        )
        return service, conversation, retrieval, repository, query, evaluator, chat

    yield build
    for service, engine in resources:
        service.close()
        engine.dispose()


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _send(service, conversation, *, key="message", content="寻找摔杯子的片段", series_ids=None, reference=None):
    return service.send_message(
        conversation.conversation_id, workspace_id="local-default",
        content=content, idempotency_key=key, series_ids=series_ids,
        reference=reference,
    )


def _collect(execution):
    events = []
    while True:
        try:
            event = execution.next_event(timeout=1)
        except Empty:
            continue
        if event is None:
            return events
        events.append(event)


class _DirectSupervisorModel:
    model_name = "supervisor-test"
    provider_name = "fake"

    def run_agent_loop(self, _prompt, *, tools, handle_text, check_control, **_kwargs):
        assert {tool.name for tool in tools} == {"search_web", "search_video_graph"}
        check_control()
        handle_text("你好，我可以直接回答。")


class _VideoSupervisorModel:
    model_name = "supervisor-test"
    provider_name = "fake"

    def __init__(self, *, include_web: bool = False):
        self.include_web = include_web

    def run_agent_loop(self, _prompt, *, tools, handle_text, check_control, **_kwargs):
        check_control()
        if self.include_web:
            web = json.loads(next(
                tool for tool in tools if tool.name == "search_web"
            ).invoke({"query": "补充信息"}))
            assert web["result_count"] == 1
        result = json.loads(next(
            tool for tool in tools if tool.name == "search_video_graph"
        ).invoke({"query": "视频里发生了什么？"}))
        assert result["ok"] is True
        handle_text(
            "网页补充 [来源](https://example.org/source)"
            if self.include_web else "未经视频审核的另一种说法。"
        )


class _WebSearcherStub:
    def search(self, _query, *, max_results, timeout_seconds):
        assert max_results == 5
        assert timeout_seconds > 0
        return [WebSearchHit(
            title="网页来源", url="https://example.org/source", snippet="补充信息。",
        )]


def test_supervisor_chat_without_ready_video_series(build_service):
    service, conversation, _, repository, *_ = build_service(
        supervisor_model=_DirectSupervisorModel(),
    )
    with repository._session_factory.begin() as session:
        session.query(SeriesRecord).update({SeriesRecord.status: "EMPTY"})

    completed = _send(service, conversation, content="你好")

    assert completed.user_message.series_ids == ()
    assert completed.assistant_message.series_ids == ()
    assert completed.assistant_message.content == "你好，我可以直接回答。"
    assert completed.assistant_message.retrieval_query is None
    assert completed.assistant_message.search_result is None
    assert _agent_message_view(completed.assistant_message).series_ids == []


@pytest.mark.parametrize("include_web", [False, True])
def test_supervisor_preserves_approved_video_answer(
    build_service, prepared_import, include_web,
):
    service, conversation, retrieval, repository, query, *_ = build_service(
        supervisor_model=_VideoSupervisorModel(include_web=include_web),
        web_searcher=_WebSearcherStub(),
    )
    videos = VideoRepository(repository._session_factory)
    report = videos.import_video_parse(prepared_import)
    with repository._session_factory.begin() as session:
        session.get(SeriesRecord, report.series_id).status = "READY"
    chunks = videos.hydrate_video_chunks(report.video_chunk_ids)
    retrieval.hits = [VideoChunkSearchHit(
        candidate=VideoChunkCandidate(
            video_chunk_id=chunk.video_chunk_id, fused_rank=index + 1,
            fused_score=1,
        ),
        selection_reason=SelectionReason(
            summary=chunk.window_summary, matched_evidence=[], limitations=[],
        ),
        evidence=chunk,
    ) for index, chunk in enumerate(chunks)]
    query.selected_ranges = tuple(
        (chunk.video_chunk_id, chunk.start_ms, chunk.end_ms)
        for chunk in chunks
    )

    completed = _send(service, conversation, content="视频里发生了什么？")

    assert completed.assistant_message.search_result is not None
    assert completed.assistant_message.content == (
        completed.assistant_message.search_result["agentReply"]["message"]
    )
    assert "未经视频审核" not in completed.assistant_message.content
    assert any(
        block["type"] == "video"
        for block in completed.assistant_message.search_result["renderBlocks"]
    )
    if include_web:
        assert completed.assistant_message.content.startswith(
            completed.assistant_message.search_result["renderBlocks"][0]["markdown"]
        )
        assert "https://example.org/source" in (
            completed.assistant_message.search_result["renderBlocks"][-1]["markdown"]
        )


@pytest.fixture
def evidence_service(build_service, prepared_import):
    resources = build_service()
    service, _, retrieval, repository, query, *_ = resources
    videos = VideoRepository(repository._session_factory)
    report = videos.import_video_parse(prepared_import)
    with repository._session_factory.begin() as session:
        session.get(SeriesRecord, report.series_id).status = "READY"
    retrieval.hits = [VideoChunkSearchHit(
        candidate=VideoChunkCandidate(video_chunk_id=chunk.video_chunk_id, fused_rank=index + 1, fused_score=1),
        selection_reason=SelectionReason(summary=chunk.window_summary, matched_evidence=[], limitations=[]),
        evidence=chunk,
    ) for index, chunk in enumerate(videos.hydrate_video_chunks(report.video_chunk_ids))]
    query.selected_ranges = tuple(
        (
            hit.evidence.video_chunk_id,
            hit.evidence.start_ms,
            hit.evidence.end_ms,
        )
        for hit in retrieval.hits
    )
    return resources


def test_first_question_titles_only_default_conversations_and_creation_replay_is_stable(build_service):
    service, custom, *_ = build_service()
    default = service.create_conversation(workspace_id="local-default", idempotency_key="default-title")
    question = "寻找爽世哭泣并说出春日影的片段"
    _send(service, default, content=question)
    updated = service.get_conversation(default.conversation_id, workspace_id="local-default")
    assert updated.title == question
    assert updated.last_message_preview == "目前没有足够的候选画面，建议补充人物或动作。"
    assert service.create_conversation(workspace_id="local-default", idempotency_key="default-title") == updated
    _send(service, custom, key="custom-message", content=question)
    assert service.get_conversation(custom.conversation_id, workspace_id="local-default").title == "测试对话"


def test_query_agent_receives_series_transcript_language_hint(build_service):
    service, conversation, _retrieval, repository, query, *_ = build_service()
    with repository._session_factory.begin() as session:
        session.scalar(select(SeriesRecord)).transcript_language_hint = "ja"

    _send(service, conversation, content="她当时的原话是什么？")

    context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])["inputs"]
    assert context["series_context"][0]["transcriptLanguageHint"] == "ja"
    assert "Transcript 通道不会翻译 Query" in query.prompts[-1]


def test_read_only_message_recovery_by_user_assistant_or_request_key(build_service):
    service, conversation, retrieval, *_ = build_service()
    result = _send(service, conversation)
    for identifier in ({"message_id": result.user_message.message_id},
                       {"message_id": result.assistant_message.message_id},
                       {"idempotency_key": "message"}):
        assert service.get_message(conversation.conversation_id, workspace_id="local-default", **identifier) == result
    assert len(retrieval.calls) == 1
    with pytest.raises(ValueError, match="exactly one"):
        service.get_message(conversation.conversation_id, workspace_id="local-default")
    with pytest.raises(AgentMessageNotFoundError):
        service.get_message(conversation.conversation_id, workspace_id="local-default", idempotency_key="not-accepted")
    with pytest.raises(AgentConversationNotFoundError):
        service.get_message(conversation.conversation_id, workspace_id="other-workspace", idempotency_key="message")


def test_reference_is_saved_and_loads_real_evidence_with_ordered_history(evidence_service):
    service, conversation, _, _, query, *_ = evidence_service
    selected = list(query.selected_ranges)
    chunk_id, start_ms, end_ms = selected[1]
    selected[1] = (chunk_id, start_ms + 1, end_ms - 1)
    query.selected_ranges = tuple(selected)
    source = _send(service, conversation)
    hits = source.assistant_message.search_result["hits"]
    reference = {"messageId": source.assistant_message.message_id,
                 "videoChunkId": hits[1]["candidate"]["videoChunkId"]}
    followup = _send(service, conversation, key="reference", content="这段的前文呢？", reference=reference)
    assert followup.user_message.reference == reference
    assert followup.assistant_message.reference is None
    assert _send(service, conversation, key="reference", content="这段的前文呢？", reference=reference) == followup
    context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])["inputs"]
    assert context["reference_context"]["evidence"]["videoChunkId"] == reference["videoChunkId"]
    assert context["reference_context"]["evidence"]["contextBeforeCompact"] == hits[1]["evidence"]["contextBeforeCompact"]
    assert context["reference_context"]["selectedRange"] == {
        "startMs": start_ms + 1,
        "endMs": end_ms - 1,
    }
    assert context["reference_context"]["sourceStatus"] == "COMPLETED"
    assert [hit["candidate"]["videoChunkId"] for hit in context["history"][1]["searchResult"]["hits"]] == [
        hit["candidate"]["videoChunkId"] for hit in hits
    ]
    assert context["history"][1]["searchResult"]["videoRanges"] == [
        {
            "videoChunkId": block["videoChunkId"],
            "startMs": block["startMs"],
            "endMs": block["endMs"],
        }
        for block in source.assistant_message.search_result["renderBlocks"]
        if block["type"] == "video"
    ]
    changed = {**reference, "videoChunkId": hits[0]["candidate"]["videoChunkId"]}
    with pytest.raises(AgentConflictError):
        _send(service, conversation, key="reference", content="这段的前文呢？", reference=changed)


@pytest.mark.parametrize("blocks", [
    None,
    [{"type": "video", "videoChunkId": "chunk-1"}],
    [{
        "type": "video", "videoChunkId": "chunk-1",
        "startMs": 999, "endMs": 1900,
    }],
    [{
        "type": "video", "videoChunkId": "chunk-1",
        "startMs": 1800, "endMs": 1700,
    }],
    [{
        "type": "video", "videoChunkId": "chunk-1",
        "startMs": 1100, "endMs": 2001,
    }],
])
def test_reference_range_rejects_legacy_or_invalid_blocks(
    caplog, blocks,
):
    caplog.set_level(logging.WARNING, logger="server.service.agent_chat")
    search_result = {} if blocks is None else {"renderBlocks": blocks}

    with pytest.raises(AgentReferenceInvalidError):
        _validated_stored_video_range(
            search_result,
            "chunk-1",
            evidence_start_ms=1000,
            evidence_end_ms=2000,
            conversation_id="conversation",
            message_id="assistant",
        )
    warning = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("event=agent_invalid_stored_video_range ")
    )
    assert "conversation_id=conversation message_id=assistant" in warning
    assert "video_chunk_id=chunk-1" in warning
    assert "evidence_start_ms=1000 evidence_end_ms=2000" in warning
    assert "action=reject_reference" in warning


def test_reference_range_preserves_valid_stored_range_without_warning(caplog):
    caplog.set_level(logging.WARNING, logger="server.service.agent_chat")

    selected_range = _validated_stored_video_range(
        {"renderBlocks": [{
            "type": "video",
            "videoChunkId": "chunk-1",
            "startMs": 1100,
            "endMs": 1900,
        }]},
        "chunk-1",
        evidence_start_ms=1000,
        evidence_end_ms=2000,
        conversation_id="conversation",
        message_id="assistant",
    )

    assert selected_range == {"startMs": 1100, "endMs": 1900}
    assert not [
        record for record in caplog.records
        if record.getMessage().startswith("event=agent_invalid_stored_video_range ")
    ]


@pytest.mark.parametrize(("render_blocks", "expected"), [
    (None, []),
    ([{
        "type": "video",
        "videoChunkId": "chunk-1",
        "startMs": 900,
        "endMs": 1900,
    }], []),
    ([{
        "type": "video",
        "videoChunkId": "unknown",
        "startMs": 1100,
        "endMs": 1900,
    }], []),
    ([{
        "type": "video",
        "videoChunkId": "chunk-1",
        "startMs": 1100,
        "endMs": 1900,
    }], [{"videoChunkId": "chunk-1", "startMs": 1100, "endMs": 1900}]),
])
def test_history_video_ranges_preserve_only_valid_stored_blocks(render_blocks, expected):
    result = {
        "hits": [{
            "candidate": {"videoChunkId": "chunk-1", "fusedRank": 1},
            "selectionReason": {},
            "evidence": {"startMs": 1000, "endMs": 2000},
        }],
    }
    if render_blocks is not None:
        result["renderBlocks"] = render_blocks

    assert _project_stored_search_result(result)["videoRanges"] == expected


@pytest.mark.parametrize("invalid", ["user-message", "missing-chunk", "other-conversation", "outside-scope"])
def test_invalid_reference_does_not_start_a_message(evidence_service, invalid):
    service, conversation, _, repository, query, *_ = evidence_service
    source = _send(service, conversation)
    reference = {"messageId": source.assistant_message.message_id,
                 "videoChunkId": source.assistant_message.search_result["hits"][0]["candidate"]["videoChunkId"]}
    target = conversation
    scope = None
    if invalid == "user-message":
        reference["messageId"] = source.user_message.message_id
    elif invalid == "missing-chunk":
        reference["videoChunkId"] = str(uuid4())
    elif invalid == "other-conversation":
        target = service.create_conversation(workspace_id="local-default", title="独立聊天", idempotency_key="other")
    else:
        library = LibraryService(LibraryRepository(repository._session_factory))
        series = library.create_series(workspace_id="local-default", name="Other Series")
        with repository._session_factory.begin() as session:
            session.get(SeriesRecord, series.series_id).status = "READY"
        scope = [series.series_id]
    before = service.list_all_messages(target.conversation_id, workspace_id="local-default")
    query_count = len(query.prompts)
    with pytest.raises(AgentReferenceInvalidError):
        _send(service, target, key="bad-reference", reference=reference, series_ids=scope)
    assert service.list_all_messages(target.conversation_id, workspace_id="local-default") == before
    assert len(query.prompts) == query_count


def test_agent_pages_count_filter_and_bind_cursor_to_query_and_scope(build_service):
    service, conversation, retrieval, *_ = build_service()
    first = _send(service, conversation)
    _send(service, conversation, key="second")
    initial = service.page_messages(conversation.conversation_id, workspace_id="local-default", limit=2)
    assert initial.total == 4 and initial.has_more and len(initial.items) == 2
    next_page = service.page_messages(conversation.conversation_id, workspace_id="local-default", limit=2,
                                      cursor=initial.next_cursor)
    assert [item.sequence_no for item in next_page.items] == [2, 3]
    assert next_page.total == 4 and not next_page.has_more and next_page.next_cursor is None
    with pytest.raises(ValueError):
        service.page_messages(conversation.conversation_id, workspace_id="local-default", limit=2,
                              cursor=initial.next_cursor, after_sequence_no=0)
    service.create_conversation(workspace_id="local-default", title="没搜索的对话", idempotency_key="unused")
    filtered = service.page_conversations(workspace_id="local-default", series_id=first.user_message.series_ids[0], query="测试")
    assert filtered.total == 1 and filtered.items[0].conversation_id == conversation.conversation_id
    assert service.page_conversations(workspace_id="local-default", query="不存在").total == 0
    assert len(retrieval.calls) == 2


def test_conversation_creation_is_idempotent_within_workspace(build_service):
    service, conversation, *_ = build_service()
    repeated = service.create_conversation(
        workspace_id="local-default", title="测试对话",
        idempotency_key="conversation",
    )
    assert repeated.conversation_id == conversation.conversation_id
    assert repeated.workspace_id == "local-default"
    assert [item.conversation_id for item in service.list_conversations(workspace_id="local-default")] == [conversation.conversation_id]
    with pytest.raises(AgentConflictError, match="different fields"):
        service.create_conversation(
            workspace_id="local-default", title="另一条对话",
            idempotency_key="conversation",
        )


def test_repository_deletes_terminal_conversation_and_returns_snapshot(build_service):
    service, conversation, _, repository, *_ = build_service(
        enforce_foreign_keys=True
    )
    _send(service, conversation)
    with repository._session_factory() as session:
        assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert session.scalar(
            select(func.count()).select_from(AgentMessageRecord)
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(AgentMessageTimingRecord)
        ) == 1
    before = repository.get_conversation(
        conversation.conversation_id,
        "local-default",
    )

    deleted = repository.delete_conversation(
        conversation.conversation_id,
        "local-default",
    )

    assert deleted == before
    assert deleted.message_count == 2
    with pytest.raises(AgentConversationNotFoundError):
        repository.get_conversation(
            conversation.conversation_id,
            "local-default",
        )
    with repository._session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(AgentMessageRecord)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(AgentMessageTimingRecord)
        ) == 0


def test_service_delete_is_workspace_scoped_and_rejects_processing_answer(build_service):
    release = Event()
    service, conversation, _, _, _, _, _ = build_service(
        chat=_ChatModel(release=release)
    )
    execution = service.start_message(
        conversation.conversation_id,
        workspace_id="local-default",
        content="找到杯子",
        idempotency_key="processing-delete",
        stream=True,
    )
    try:
        with pytest.raises(AgentConversationNotFoundError):
            service.delete_conversation(
                conversation.conversation_id,
                workspace_id="another-workspace",
            )
        with pytest.raises(AgentMessageInProgressError):
            service.delete_conversation(
                conversation.conversation_id,
                workspace_id="local-default",
            )
        assert service.get_conversation(
            conversation.conversation_id,
            workspace_id="local-default",
        ).message_count == 2
    finally:
        release.set()
    execution.result()

    deleted = service.delete_conversation(
        conversation.conversation_id,
        workspace_id="local-default",
    )
    assert deleted.conversation_id == conversation.conversation_id
    assert deleted.message_count == 2
    with pytest.raises(AgentConversationNotFoundError):
        service.get_conversation(
            conversation.conversation_id,
            workspace_id="local-default",
        )


def test_conversation_reads_and_execution_do_not_cross_workspaces(build_service):
    service, conversation, retrieval, _, query, *_ = build_service()
    assert service.list_conversations(workspace_id="another-workspace") == []
    for read in [service.get_conversation, service.list_messages, service.list_all_messages]:
        with pytest.raises(AgentConversationNotFoundError):
            read(conversation.conversation_id, workspace_id="another-workspace")
    with pytest.raises(AgentConversationNotFoundError):
        service.send_message(
            conversation.conversation_id, workspace_id="another-workspace",
            content="寻找摔杯子的片段", idempotency_key="message",
        )
    assert not retrieval.calls and not query.prompts
    assert service.list_all_messages(conversation.conversation_id, workspace_id="local-default") == []


@pytest.mark.parametrize("second_query", ["补充摔杯子", None])
def test_reviewed_chat_is_persisted_and_idempotent(build_service, second_query):
    service, conversation, retrieval, _, query, evaluator, chat = build_service(
        query=_QueryModel(queries=("第一次改写", second_query)),
        evaluator=_EvaluatorModel(approve_after=2),
    )
    first = _send(service, conversation)
    repeated = _send(service, conversation)
    messages = service.list_messages(conversation.conversation_id, workspace_id="local-default")
    assert first.assistant_message.message_id == repeated.assistant_message.message_id
    assert len(messages) == 2
    assert [m.status for m in messages] == ["COMPLETED", "COMPLETED"]
    assert messages[0].model_name is None
    assert messages[1].model_name == "chat-test"
    assert messages[1].content == "目前没有足够的候选画面，建议补充人物或动作。"
    assert messages[1].search_result["hits"] == []
    timing = messages[1].timing
    assert timing is not None and timing == first.assistant_message.timing
    assert timing == repeated.assistant_message.timing
    assert [stage.phase.value for stage in timing.stages] == [
        "QUEUED", "RETRIEVING", "EVALUATING", "RETRIEVING",
        "EVALUATING", "RESPONDING",
    ]
    assert all(
        type(stage.duration_ms) is int and stage.duration_ms >= 0
        for stage in timing.stages
    )
    assert timing.total_duration_ms == sum(
        stage.duration_ms for stage in timing.stages
    )
    assert messages[0].timing is None
    assert [call[0] for call in retrieval.calls] == ["第一次改写"] + ([second_query] if second_query else [])
    assert evaluator.calls == 2 and len(chat.prompts) == 1
    context = json.loads(query.prompts[1].rsplit("\n\n", 1)[1])
    assert context["original_plan"] == "第 1 轮检索并核验动作。"
    assert context["turns"][0]["draft"]["message"] == "第 1 轮待审核草稿。"
    assert context["turns"][0]["review"]["advice"] == "补充摔杯子的动作，重新核验。"
    with pytest.raises(AgentConflictError, match="different request fields"):
        service.send_message(conversation.conversation_id, workspace_id="local-default", content="寻找摔杯子的片段", top_k=7, idempotency_key="message")
    with pytest.raises(AgentConflictError, match="different request fields"):
        service.send_message(
            conversation.conversation_id,
            workspace_id="local-default",
            content="寻找摔杯子的片段",
            idempotency_key="message",
            max_model_calls_per_turn=7,
        )


def test_per_message_run_options_are_bounded_persisted_and_passed_to_runtime(
    build_service,
):
    service, conversation, _, repository, _, _, _ = build_service()
    observed = []
    original_kick = service._runtime.kick

    def capture_kick(*args, **kwargs):
        observed.append((
            kwargs["max_model_calls_per_turn"],
            kwargs["max_tool_calls_per_turn"],
            kwargs["max_loop_turns"],
        ))
        return original_kick(*args, **kwargs)

    service._runtime.kick = capture_kick
    result = service.send_message(
        conversation.conversation_id,
        workspace_id="local-default",
        content="寻找摔杯子的片段",
        idempotency_key="custom-run-options",
        max_model_calls_per_turn=9,
        max_tool_calls_per_turn=33,
    )

    assert result.assistant_message.status == "COMPLETED"
    assert observed == [(9, 33, None)]
    with repository._session_factory() as session:
        user = session.scalar(select(AgentMessageRecord).where(
            AgentMessageRecord.idempotency_key == "custom-run-options",
        ))
        assert user is not None
        assert user.requested_run_options == {
            "max_model_calls_per_turn": 9,
            "max_tool_calls_per_turn": 33,
        }
    service.send_message(
        conversation.conversation_id,
        workspace_id="local-default",
        content="再找一个片段",
        idempotency_key="auto-loop-options",
        max_loop_turns=0,
    )
    service.send_message(
        conversation.conversation_id,
        workspace_id="local-default",
        content="换个片段",
        idempotency_key="custom-loop-options",
        max_loop_turns=100,
    )
    assert observed[-2:] == [(6, 24, 0), (6, 24, 100)]
    with repository._session_factory() as session:
        users = session.scalars(select(AgentMessageRecord).where(
            AgentMessageRecord.idempotency_key.in_(["auto-loop-options", "custom-loop-options"]),
        )).all()
        snapshots = {user.idempotency_key: user.requested_run_options for user in users}
    assert snapshots["auto-loop-options"]["max_loop_turns"] == 0
    assert snapshots["custom-loop-options"]["max_loop_turns"] == 100
    with pytest.raises(AgentConflictError, match="different request fields"):
        service.send_message(
            conversation.conversation_id,
            workspace_id="local-default",
            content="换个片段",
            idempotency_key="custom-loop-options",
            max_loop_turns=0,
        )
    with pytest.raises(ValueError, match="between 0 and 100"):
        service.send_message(
            conversation.conversation_id,
            workspace_id="local-default",
            content="轮次过高",
            idempotency_key="over-loop-limit",
            max_loop_turns=101,
        )
    with pytest.raises(ValueError, match="between 1 and 12"):
        service.send_message(
            conversation.conversation_id,
            workspace_id="local-default",
            content="再找一个片段",
            idempotency_key="over-model-limit",
            max_model_calls_per_turn=13,
        )


def test_third_continue_persists_failure_and_never_calls_chat(build_service):
    service, conversation, _, _, _, evaluator, chat = build_service(
        evaluator=_EvaluatorModel(approve_after=4),
        runtime_kwargs={"limits_enabled": True},
    )
    with pytest.raises(AgentModelInvocationError, match="after 3 turns"):
        _send(service, conversation)
    assert evaluator.calls == 3 and not chat.prompts
    messages = service.list_all_messages(conversation.conversation_id, workspace_id="local-default")
    assert [m.status for m in messages] == ["COMPLETED", "FAILED"]
    assert messages[1].search_result is None and messages[1].model_name == "chat-test"
    assert messages[1].timing is not None
    assert [stage.phase.value for stage in messages[1].timing.stages] == [
        "QUEUED", "RETRIEVING", "EVALUATING", "RETRIEVING",
        "EVALUATING", "RETRIEVING", "EVALUATING",
    ]


def test_context_budget_failure_is_persisted_with_a_specific_public_error(build_service):
    service, conversation, _, _, query, evaluator, chat = build_service(
        runtime_kwargs={"max_prompt_chars": 100},
    )

    with pytest.raises(AgentContextBudgetError, match="context character budget"):
        _send(service, conversation)

    messages = service.list_all_messages(
        conversation.conversation_id, workspace_id="local-default",
    )
    assert messages[-1].status == "FAILED"
    assert messages[-1].last_error.code == "AGENT_CONTEXT_BUDGET_EXCEEDED"
    assert "精简系列背景" in messages[-1].last_error.message
    assert query.prompts == [] and evaluator.calls == 0 and chat.prompts == []


def test_next_kick_reads_only_completed_conversation_history(build_service):
    service, conversation, _, _, query, _, _ = build_service()
    first = _send(service, conversation)
    _send(service, conversation, key="second", content="再找一段")
    context = json.loads(query.prompts[1].rsplit("\n\n", 1)[1])
    assert context["turn_number"] == 1 and context["turns"] == []
    history = context["inputs"]["history"]
    assert history[0] == {
        "role": "USER", "content": first.user_message.content,
        "seriesIds": list(first.user_message.series_ids),
        "messageId": first.user_message.message_id, "status": "COMPLETED",
        "reference": None, "searchResult": None,
    }
    assert history[1] == {
        "role": "ASSISTANT", "content": first.assistant_message.content,
        "seriesIds": list(first.assistant_message.series_ids),
        "messageId": first.assistant_message.message_id, "status": "COMPLETED",
        "reference": None,
        "searchResult": {
            "query": first.assistant_message.search_result["query"],
            "seriesIds": first.assistant_message.search_result["seriesIds"],
            "resultCount": 0, "hits": [], "videoRanges": [],
            "partial": False, "warnings": [],
        },
    }
    with pytest.raises(AgentConversationNotFoundError):
        service.send_message(conversation.conversation_id, workspace_id="other-workspace", content="隔离", idempotency_key="other")


def test_all_scope_is_frozen_across_turns_and_replay_then_refreshes_for_next_kick(
    build_service, monkeypatch,
):
    service, conversation, retrieval, repository, query, _, _ = build_service(
        evaluator=_EvaluatorModel(approve_after=2),
    )
    library = LibraryService(LibraryRepository(repository._session_factory))
    original_id = library.list_series(workspace_id="local-default")[0].series_id
    run_structured = query.run_structured
    added_ids = []

    def query_with_new_series(*args, **kwargs):
        draft = run_structured(*args, **kwargs)
        if not added_ids:
            added = library.create_series(workspace_id="local-default", name="Added mid-loop")
            with repository._session_factory.begin() as session:
                session.get(SeriesRecord, added.series_id).status = "READY"
            added_ids.append(added.series_id)
        return draft

    monkeypatch.setattr(query, "run_structured", query_with_new_series)
    first = _send(service, conversation, series_ids=[])
    assert [call[1] for call in retrieval.calls] == [[original_id], [original_id]]
    assert first.user_message.series_ids == first.assistant_message.series_ids == (original_id,)
    assert all(
        json.loads(prompt.rsplit("\n\n", 1)[1])["inputs"]["series_ids"] == [original_id]
        for prompt in query.prompts
    )

    # An identical "all" request returns its saved answer, even after readiness changes.
    with repository._session_factory.begin() as session:
        session.get(SeriesRecord, original_id).status = "INDEXING"
    assert _send(service, conversation) == first
    assert len(retrieval.calls) == 2
    with repository._session_factory.begin() as session:
        session.get(SeriesRecord, original_id).status = "READY"

    second = _send(service, conversation, key="next-all", series_ids=[])
    assert second.user_message.series_ids == tuple(sorted([original_id, added_ids[0]]))
    assert retrieval.calls[-1][1] == list(second.user_message.series_ids)
    context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])
    assert [item["seriesIds"] for item in context["inputs"]["history"]] == [[original_id], [original_id]]


def test_next_message_can_choose_another_scope_without_reusing_old_evidence(build_service):
    service, conversation, retrieval, repository, query, _, _ = build_service()
    library = LibraryService(LibraryRepository(repository._session_factory))
    original_id = library.list_series(workspace_id="local-default")[0].series_id
    added = library.create_series(workspace_id="local-default", name="Second animation")
    with repository._session_factory.begin() as session:
        session.get(SeriesRecord, added.series_id).status = "READY"

    first = _send(service, conversation, series_ids=[original_id])
    second = _send(service, conversation, key="second", series_ids=[added.series_id])
    assert [call[1] for call in retrieval.calls] == [[original_id], [added.series_id]]
    assert first.assistant_message.series_ids == (original_id,)
    assert second.assistant_message.series_ids == (added.series_id,)
    context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])
    assert context["turns"] == [] and context["inputs"]["series_ids"] == [added.series_id]
    assert all(item["seriesIds"] == [original_id] for item in context["inputs"]["history"])
    with pytest.raises(AgentConflictError):
        _send(service, conversation, series_ids=[added.series_id])


def test_tool_answer_is_atomic_and_disconnect_does_not_cancel(build_service):
    release = Event()
    service, conversation, _, _, _, _, _ = build_service(chat=_ChatModel(release=release))
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default", content="找到杯子", idempotency_key="stream", stream=True)
    try:
        events = [execution.next_event(timeout=2) for _ in range(4)]
        assert [event["type"] for event in events] == [
            "answer.started", "answer.progress", "answer.progress", "answer.progress",
        ]
        assert [event["phase"] for event in events if event["type"] == "answer.progress"] == [
            "RETRIEVING", "EVALUATING", "RESPONDING",
        ]
        assert all(event["conversationId"] == conversation.conversation_id for event in events)
        assert [event["sequence"] for event in events] == list(range(1, 5))
        assert all(isinstance(event["occurredAt"], int) for event in events)
        progress_events = [
            event for event in events if event["type"] == "answer.progress"
        ]
        assert [
            [stage["phase"].value for stage in event["timing"]["stages"]]
            for event in progress_events
        ] == [
            ["QUEUED"],
            ["QUEUED", "RETRIEVING"],
            ["QUEUED", "RETRIEVING", "EVALUATING"],
        ]
        assert all(
            event["timing"]["total_duration_ms"] is None
            for event in progress_events
        )
        assert not execution.future.done()
        with pytest.raises(Empty):
            execution.next_event(timeout=0.05)
        processing = service.list_all_messages(
            conversation.conversation_id, workspace_id="local-default",
        )[-1]
        assert processing.status == "PROCESSING"
        assert processing.timing is not None
        assert processing.timing.total_duration_ms is None
        assert [stage.phase.value for stage in processing.timing.stages] == [
            "QUEUED", "RETRIEVING", "EVALUATING",
        ]
        with pytest.raises(AgentMessageInProgressError):
            service.start_message(conversation.conversation_id, workspace_id="local-default", content="找到杯子", idempotency_key="stream")
        with pytest.raises(AgentMessageInProgressError):
            service.start_message(conversation.conversation_id, workspace_id="local-default", content="另一个问题", idempotency_key="other")
        execution.detach()
        assert not execution.cancellation.is_set()
    finally:
        release.set()
    assert execution.result().assistant_message.status == "COMPLETED"


def test_completed_event_observes_committed_snapshot(build_service):
    service, conversation, _, _, _, _, _ = build_service()
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default", content="找杯子", idempotency_key="stream", stream=True)
    events = _collect(execution)
    assert events[-1]["type"] == "answer.completed"
    completed = execution.result().assistant_message
    persisted = service.list_all_messages(conversation.conversation_id, workspace_id="local-default")[-1]
    assert persisted == completed
    assert events[-1]["assistantMessage"]["status"] == "COMPLETED"
    assert [event["type"] for event in events].count("answer.evidence") == 1
    evidence = next(event for event in events if event["type"] == "answer.evidence")
    assert evidence["searchResult"] == completed.search_result
    assert completed.search_result["retrievalTrace"] == [{
        "query": "第一次改写",
        "topK": service.default_top_k,
        "strategyVersion": "query-adaptive-three-channel-rrf-v2",
        "requestedWeights": {"transcript": 0.1, "semantic": 0.5, "visual": 0.4},
        "effectiveWeights": {"transcript": 0.1, "semantic": 0.5, "visual": 0.4},
        "resultCount": 0,
        "videoChunkIds": [],
    }]
    blocks = [event["block"] for event in events if event["type"] == "answer.block"]
    assert blocks == completed.search_result["renderBlocks"]
    assert [event["type"] for event in events[-4:]] == [
        "answer.evidence", "answer.block", "answer.delta", "answer.completed",
    ]
    deltas = [event["delta"] for event in events if event["type"] == "answer.delta"]
    assert deltas == [completed.content]


@pytest.mark.parametrize("failure", ["chat", "commit"])
def test_failure_has_no_completed_event_and_never_persists_unvalidated_chat_text(
    build_service, monkeypatch, caplog, failure,
):
    caplog.set_level(logging.ERROR, logger="server.service.agent_chat")
    service, conversation, _, repository, _, _, _ = build_service(chat=_ChatModel(fail=failure == "chat"))
    if failure == "commit":
        def fail_commit(*_args, **_kwargs):
            raise RuntimeError("database commit failed")
        monkeypatch.setattr(repository, "complete_assistant_message", fail_commit)
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default", content="找杯子", idempotency_key="stream", stream=True)
    events = _collect(execution)
    with pytest.raises((RuntimeError, AgentModelInvocationError)):
        execution.result()
    assert events[-1]["type"] == "answer.failed"
    assert "answer.completed" not in [event["type"] for event in events]
    failed = service.list_all_messages(conversation.conversation_id, workspace_id="local-default")[-1]
    assert failed.status == "FAILED"
    assert failed.content is None
    assert failed.search_result is None
    assert [event for event in events if event["type"] == "answer.delta"] == []
    assert [event for event in events if event["type"] == "answer.evidence"] == []
    assert [event for event in events if event["type"] == "answer.block"] == []
    assert failed.phase is None and failed.version > 1
    assert failed.timing is not None
    assert failed.timing.total_duration_ms == sum(
        stage.duration_ms for stage in failed.timing.stages
    )
    assert failed.timing.stages[-1].phase.value == "RESPONDING"
    assert asdict(failed.last_error) == events[-1]["error"]
    assert "private upstream failure" not in events[-1]["error"]["message"]
    failure_log = next(
        record for record in caplog.records
        if record.name == "server.service.agent_chat"
        and record.getMessage().startswith("agent_execution_failed ")
    )
    failure_message = failure_log.getMessage()
    assert f"conversation_id={conversation.conversation_id}" in failure_message
    assert f"message_id={failed.message_id}" in failure_message
    assert "phase=RESPONDING" in failure_message
    assert "partial_chars=0" in failure_message
    assert "evidence_hits=0" in failure_message
    assert "root_error_type=RuntimeError" in failure_message
    if failure == "chat":
        assert failure_log.exc_info is None
        assert "private upstream failure" not in caplog.text
    else:
        assert failure_log.exc_info is not None


def test_unconfirmed_terminal_write_emits_interrupted_and_recovers_processing_snapshot(build_service, monkeypatch):
    service, conversation, _, repository, *_ = build_service(chat=_ChatModel(fail=True))
    def unavailable_failure_write(*_args, **_kwargs):
        raise RuntimeError("terminal database write unavailable")
    monkeypatch.setattr(repository, "fail_assistant_message", unavailable_failure_write)
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default",
                                      content="找画面", idempotency_key="unknown-terminal", stream=True)
    events = _collect(execution)
    with pytest.raises(AgentModelInvocationError):
        execution.result()
    assert events[-1]["type"] == "answer.interrupted"
    assert not {"userMessage", "assistantMessage"} & set(events[-1])
    assert not {"answer.completed", "answer.failed"} & {event["type"] for event in events}
    recovered = service.get_message(conversation.conversation_id, workspace_id="local-default",
                                    idempotency_key="unknown-terminal").assistant_message
    assert recovered.status == "PROCESSING" and recovered.search_result is None
    assert (recovered.content or "") == "".join(
        event["delta"] for event in events if event["type"] == "answer.delta"
    )


def test_slow_consumer_receives_one_atomic_tool_answer(build_service):
    service, conversation, *_ = build_service()
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default",
                                      content="找画面", idempotency_key="slow-reader", stream=True)
    result = execution.result()
    events = _collect(execution)
    assert [event["type"] for event in events].count("answer.delta") == 1
    assert [event["type"] for event in events].count("answer.block") == 1
    assert [event["delta"] for event in events if event["type"] == "answer.delta"] == [
        result.assistant_message.content,
    ]
    assert events[-1]["type"] == "answer.completed"
    assert result.assistant_message.status == "COMPLETED"
    assert result.assistant_message.content == "目前没有足够的候选画面，建议补充人物或动作。"
    assert service.get_message(conversation.conversation_id, workspace_id="local-default",
                               idempotency_key="slow-reader") == result


def test_shutdown_requests_stop_and_waits_before_resources_close(build_service):
    release = Event()
    evaluator = _EvaluatorModel()
    closed = []
    evaluator.close = lambda: closed.append(True)
    service, conversation, _, _, _, _, _ = build_service(
        evaluator=evaluator,
        chat=_ChatModel(release=release),
    )
    execution = service.start_message(conversation.conversation_id, workspace_id="local-default", content="找杯子", idempotency_key="shutdown", stream=True)
    try:
        for _ in range(4):
            execution.next_event(timeout=2)
        execution.cancellation.set()
    finally:
        release.set()
    service.close()
    assert closed == [True]
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        execution.result()
    assert service.list_all_messages(conversation.conversation_id, workspace_id="local-default")[-1].status == "FAILED"
    with pytest.raises(AgentMessageInProgressError, match="shutting down"):
        _send(service, conversation, key="late")


def test_global_settings_reload_applies_to_new_messages_only(build_service):
    release = Event()
    entered = Event()
    old_chat = _ChatModel(release=release)
    original_run = old_chat.run_agent_loop

    def wait_in_old_runtime(*args, **kwargs):
        entered.set()
        return original_run(*args, **kwargs)

    old_chat.run_agent_loop = wait_in_old_runtime
    initial_config = AgentServiceConfig()
    service, conversation, retrieval, repository, *_ = build_service(
        chat=old_chat, service_kwargs={"config": initial_config},
    )
    old_runtime = service._runtime
    config = initial_config
    revision = "before"
    service._config_loader = lambda: (config, revision)
    service._config_revision = revision
    service._runtime_factory = lambda current: AgentRuntime(
        _QueryModel(), _EvaluatorModel(), _ChatModel(), retrieval.search,
        evaluator_stage_timeout_seconds=current.evaluator_stage_timeout_seconds,
    )
    history_limits = []
    original_history = repository.list_recent_completed_messages

    def record_history_limit(*args, **kwargs):
        history_limits.append(kwargs["limit"])
        return original_history(*args, **kwargs)

    repository.list_recent_completed_messages = record_history_limit
    before = service.start_message(
        conversation.conversation_id, workspace_id="local-default",
        content="改配置前", idempotency_key="before",
    )
    try:
        assert entered.wait(5)
        config = replace(
            initial_config, history_message_limit=2,
            evaluator_stage_timeout_seconds=240,
            maximum_concurrent_kicks=7,
        )
        revision = "after"
        next_conversation = service.create_conversation(
            workspace_id="local-default", idempotency_key="next-conversation",
        )
        after = service.start_message(
            next_conversation.conversation_id, workspace_id="local-default",
            content="改配置后", idempotency_key="after",
        )
        assert after.result().assistant_message.status == "COMPLETED"
        assert service._runtime is not old_runtime
        assert service._runtime._evaluator_stage_timeout == 240
        assert service.active_maximum_concurrent_kicks == initial_config.maximum_concurrent_kicks
        assert history_limits == [initial_config.history_message_limit, 2]
        updated_runtime = service._runtime
        config = replace(config, maximum_concurrent_kicks=8)
        revision = "concurrency-only"
        service.message_options()
        assert service._runtime is updated_runtime
        assert service.active_maximum_concurrent_kicks == initial_config.maximum_concurrent_kicks
    finally:
        release.set()
    assert before.result().assistant_message.status == "COMPLETED"


@pytest.mark.parametrize("final_status", ["COMPLETED", "FAILED"])
def test_agent_complete_snapshot_reads_after_500_and_same_sequence_updates(
    tmp_path, final_status
) -> None:
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'long-conversation.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    repository = AgentRepository(session_factory)
    library = LibraryService(LibraryRepository(session_factory))
    series = library.create_series(workspace_id="workspace-1", name="Long Conversation")
    service = AgentChatService(
        repository,
        library,
        AgentRuntime(_QueryModel(), _EvaluatorModel(), _ChatModel(), _RetrievalStub().search),
    )
    conversation = service.create_conversation(
        workspace_id="workspace-1",
        title="History",
        idempotency_key="conversation",
    )
    with session_factory.begin() as db_session:
        db_session.add_all(
            AgentMessageRecord(
                message_id=str(uuid4()),
                conversation_id=conversation.conversation_id,
                sequence_no=index,
                role="USER" if index % 2 == 0 else "ASSISTANT",
                status="COMPLETED",
                content=f"history {index}",
                series_ids=[series.series_id],
                requested_series_ids=[series.series_id] if index % 2 == 0 else None,
                model_name=None if index % 2 == 0 else "historical-model",
            )
            for index in range(500)
        )
        record = db_session.get(AgentConversationRecord, conversation.conversation_id)
        assert record is not None
        record.next_message_sequence = 500

    now = 1_789_454_400_000
    started = repository.start_message(
        conversation_id=conversation.conversation_id,
        workspace_id="workspace-1",
        content="new question",
        model_name="current-model",
        requested_top_k=8,
        requested_run_options={
            "max_model_calls_per_turn": 6,
            "max_tool_calls_per_turn": 24,
        },
        series_ids=[series.series_id],
        requested_series_ids=[series.series_id],
        idempotency_key="new-message",
        user_message_id=str(uuid4()),
        assistant_message_id=str(uuid4()),
        now=now,
    )
    page = service.list_messages(
        conversation.conversation_id, workspace_id="workspace-1", limit=500
    )
    snapshot = service.list_all_messages(
        conversation.conversation_id, workspace_id="workspace-1"
    )
    assert len(page) == 500 and page[-1].sequence_no == 499
    assert len(snapshot) == 502
    assert snapshot[-1].sequence_no == 501
    assert snapshot[-1].status == "PROCESSING"
    assert snapshot[-1].model_name == "current-model"

    if final_status == "COMPLETED":
        repository.complete_assistant_message(
            started.assistant_message.message_id,
            content="answer",
            retrieval_query="query",
            search_result={"hits": []},
            now=1_789_454_399_000,
        )
    else:
        repository.fail_assistant_message(
            started.assistant_message.message_id, error="failed",
            now=1_789_454_399_000,
        )
    final = service.list_all_messages(
        conversation.conversation_id, workspace_id="workspace-1"
    )
    assert len(final) == 502
    assert final[-1].message_id == snapshot[-1].message_id
    assert final[-1].sequence_no == 501
    assert final[-1].status == final_status
    assert final[-1].model_name == "current-model"
    assert final[-1].timing is not None
    assert final[-1].timing.total_duration_ms == 0
    assert [stage.phase.value for stage in final[-1].timing.stages] == ["QUEUED"]
    assert all(message.timing is None for message in final[:-1])
    with pytest.raises(AgentConversationNotFoundError):
        service.list_all_messages(
            conversation.conversation_id, workspace_id="another-workspace"
        )
