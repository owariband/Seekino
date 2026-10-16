from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from contextlib import contextmanager
from queue import Empty
from threading import Event
from uuid import uuid4

import pytest
from langchain_core.tools import BaseTool
from sqlalchemy import delete, event, func, inspect, select, text

from agent.chat.common.errors import (
    AgentContextBudgetError, AgentExecutionTimeoutError, AgentModelInvocationError,
)
from agent.chat.common.state import AnswerPhase
from agent.chat.supervisor import SupervisorRuntime
from agent.chat.video_search.context import _project_stored_search_result
from agent.chat.video_search.runtime import AgentRuntime
from gateway.web_search import WebSearchHit, WebSearcher
from gateway.usage import capture_model_usage, model_call_context, track_model_call
from server.api.agent import _agent_message_view
from server.config.agent import AgentServiceConfig
from server.database import (
    DatabaseEnvironment, create_database_engine, create_schema, create_session_factory,
)
from server.model.agent import (
    AgentConversation,
    AgentMessage,
)
from server.model.video import Series, Video, VideoChunkInfo
from server.model.dto.agentDTO import AgentMessageTimingState, AgentStageTimingState
from server.repository.agent import (
    AgentConflictError, AgentConversationNotFoundError, AgentMessageInProgressError,
    AgentRepository, AgentReferenceInvalidError, AgentMessageNotFoundError,
)
from server.repository.library import LibraryRepository, SeriesNotFoundError
from server.repository.video import SeriesNotReadyError, VideoRepository
from server.schema import (
    AgentReply, SelectionReason, VideoChunkCandidate, VideoChunkSearchHit, VideoChunkSearchResult,
)
from server.service.agent_chat import (
    AgentAttachmentInputUnavailableError, AgentChatService, _validated_stored_video_range,
)
from server.service.library import LibraryService


@dataclass
class _RetrievalStub:
    calls: list[tuple[str, list[str], int, str]] = field(default_factory=list)
    hits: list[VideoChunkSearchHit] = field(default_factory=list)

    def search(
        self,
        query,
        *,
        tenant_id,
        series_ids,
        top_k,
        lexical_weight=None,
        text_weight=None,
        vision_weight=None,
    ):
        if any(
            weight is not None
            for weight in (lexical_weight, text_weight, vision_weight)
        ):
            assert None not in (lexical_weight, text_weight, vision_weight)
            assert lexical_weight + text_weight + vision_weight == pytest.approx(1.0)
        self.calls.append((query, list(series_ids), top_k, tenant_id))
        return VideoChunkSearchResult(
            query=query, series_ids=list(series_ids),
            agent_reply=AgentReply(message="底层检索完成。", result_count=len(self.hits)), hits=self.hits,
        )


def _read_all(tool, video_chunk_id: str, *, collection: str) -> dict:
    overview = json.loads(tool.invoke({
        "collection": collection, "reference": video_chunk_id,
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
                    "collection": collection, "reference": video_chunk_id,
                    "section": name,
                    "start": start,
                    "end": end,
                }))
                if "error" not in page:
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


def _write_canvas(tools_by_name, operation: str, fields: dict) -> dict:
    collection = "draft" if "draft" in operation else "approved"
    current = json.loads(tools_by_name["get_context_canvas"].invoke({"collection": collection}))
    receipt = json.loads(tools_by_name["write_context_canvas"].invoke({
        "batch_id": uuid4().hex, "expected_revision": current["revision"],
        "operations": [{"operation": operation, **fields}],
    }))
    assert "error" not in receipt
    return receipt["results"][0]


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
                    "lexical": 0.1,
                    "semantic": 0.5,
                    "visual": 0.4,
                },
            })
        for video_chunk_id, start_seconds, end_seconds in self.selected_ranges:
            _read_all(tools_by_name["get_context_canvas"], video_chunk_id, collection="retrieval")
            _write_canvas(tools_by_name, "upsert_draft_item", {
                "video_chunk_id": video_chunk_id,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
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
            page = json.loads(tools_by_name["get_context_canvas"].invoke({
                "collection": "draft",
                "cursor": cursor,
            }))
            item_ids.extend(item["video_chunk_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        for video_chunk_id in item_ids:
            _read_all(tools_by_name["get_context_canvas"], video_chunk_id, collection="draft")
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
        image_urls=(), initial_messages=None,
    ):
        tools_by_name = {tool.name: tool for tool in tools}
        assert set(tools_by_name) == {"get_context_canvas", "write_context_canvas"}
        assert set(tools_by_name["write_context_canvas"].args_schema.model_fields) == {
            "batch_id", "expected_revision", "operations",
        }
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
            assert _write_canvas(tools_by_name, "upsert_video_item", item) == {
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
        web_searcher=None, realtime_runtime=None,
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
            query,
            evaluator,
            chat,
            retrieval.search,
            **{"retrieval_channel": "lexical", **(runtime_kwargs or {})},
        )
        runtime = (
            SupervisorRuntime(
                video_runtime, supervisor_model, web_searcher or WebSearcher(),
                max_web_results=5, web_timeout_seconds=12,
                execution_timeout_seconds=60, max_tool_result_bytes=16_384,
                max_prompt_chars=200_000,
                realtime_runtime=realtime_runtime,
            )
            if supervisor_model is not None else video_runtime
        )
        service = AgentChatService(
            repository, library, runtime, **(service_kwargs or {}),
        )
        resources.append((service, engine))
        series = library.create_series(tenant_id="local-default", name="Test Series")
        with factory.begin() as session:
            session.get(Series, series.series_id).status = "READY"
        conversation = service.create_conversation(
            tenant_id="local-default", title="测试对话",
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


def _send(
    service,
    conversation,
    *,
    key="message",
    content="寻找摔杯子的片段",
    series_ids=None,
    reference=None,
    extra_context=None,
    attachments=None,
):
    return service.send_message(
        conversation.conversation_id, tenant_id="local-default",
        content=content, idempotency_key=key, series_ids=series_ids,
        reference=reference, extra_context=extra_context, attachments=attachments,
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


def test_chat_tracing_binds_message_identity_in_the_actual_executor(build_service):
    from threading import get_ident

    main_thread = get_ident()
    observed, thread_ids = [], []

    def observe(call):
        observed.append(call)
        thread_ids.append(get_ident())

    class Tracing:
        @contextmanager
        def scope(self, context):
            with model_call_context(context), capture_model_usage(observe, combine=True):
                yield

    class QueryModel(_QueryModel):
        def run_structured(self, *args, **kwargs):
            with track_model_call(provider="fixture", model=self.model_name, operation="query"):
                return super().run_structured(*args, **kwargs)

    service, conversation, *_ = build_service(
        query=QueryModel(), service_kwargs={"tracing": Tracing()},
    )
    first = _send(service, conversation)
    repeated = _send(service, conversation)
    assert repeated == first
    assert len(observed) == 1
    assert thread_ids[0] != main_thread
    context = observed[0].context
    assert context["tenant_id"] == "local-default"
    assert context["conversation_id"] == conversation.conversation_id
    assert context["user_message_id"] == first.user_message.message_id
    assert context["assistant_message_id"] == first.assistant_message.message_id
    assert context["run_id"] == first.assistant_message.message_id
    assert "content" not in context


@pytest.mark.parametrize("failed", [False, True])
def test_chat_sql_capture_runs_in_executor_and_preserves_terminal_result(build_service, failed):
    from threading import get_ident
    from gateway.usage import TokenUsage
    from server.model.parse_usage import ModelUsage
    from server.repository.model_usage import ModelUsageRepository
    from server.service.parse_usage import ParseUsageService

    threads = []
    main_thread = get_ident()

    class QueryModel(_QueryModel):
        def run_structured(self, *args, **kwargs):
            threads.append(get_ident())
            with track_model_call(provider="fixture", model=self.model_name, operation="query") as tracker:
                tracker.usage = TokenUsage(10, 2, 12, None, None)
                if failed:
                    raise RuntimeError("injected query failure")
                return super().run_structured(*args, **kwargs)

    service, conversation, _, repository, *_ = build_service(query=QueryModel())
    service._parse_usage = ParseUsageService(ModelUsageRepository(repository._session_factory))
    if failed:
        with pytest.raises(AgentModelInvocationError, match="Query Agent model"):
            _send(service, conversation)
        first = _send(service, conversation)
    else:
        first = _send(service, conversation)
        assert _send(service, conversation) == first
    assert len(threads) == 1 and threads[0] != main_thread
    assert first.assistant_message.status == ("FAILED" if failed else "COMPLETED")
    with repository._session_factory() as session:
        rows = session.scalars(select(ModelUsage)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.tenant_id == "local-default" and row.task_type == "chat_retrieval"
        assert row.run_id == first.assistant_message.message_id
        assert row.user_id == "local-user"
        assert row.metadata_["status"] == ("failed" if failed else "completed")
        assert row.metadata_["context"]["conversation_id"] == conversation.conversation_id
        assert row.usage["tokens"]["total_tokens"] == 12


class _DirectSupervisorModel:
    model_name = "supervisor-test"
    provider_name = "fake"

    def __init__(self):
        self.prompts = []
        self.turns = []
        self.confirmed = []
        self.discarded = []
        self.deleted = []

    def run_agent(
        self, prompt, *, thread_id, message_id, user_message, history,
        tools, handle_text, check_control, **_kwargs,
    ):
        self.prompts.append(prompt)
        self.turns.append((thread_id, message_id, user_message, history))
        assert {tool.name for tool in tools} == {
            "search_web", "search_video_graph", "get_context_canvas", "submit_video_item",
            "remove_video_item",
        }
        check_control()
        handle_text("你好，我可以直接回答。")

    def confirm_turn(self, thread_id, message_id, content):
        self.confirmed.append((thread_id, message_id, content))

    def discard_turn(self, thread_id, message_id):
        self.discarded.append((thread_id, message_id))

    def delete_thread(self, thread_id):
        self.deleted.append(thread_id)


class _VideoSupervisorModel(_DirectSupervisorModel):
    model_name = "supervisor-test"
    provider_name = "fake"

    def __init__(self, *, include_web: bool = False, merge_adjacent: bool = False, revise: bool = False):
        super().__init__()
        self.include_web = include_web
        self.merge_adjacent = merge_adjacent
        self.revise = revise
        self.submitted_items = []
        self.answer = "终审已根据完整时间线收紧片段，保留支撑结论的完整内容。"

    def run_agent(self, _prompt, *, tools, handle_text, check_control, **_kwargs):
        self.prompts.append(_prompt)
        self.turns.append((
            _kwargs["thread_id"], _kwargs["message_id"],
            _kwargs["user_message"], _kwargs["history"],
        ))
        check_control()
        by_name = {tool.name: tool for tool in tools}
        if self.include_web:
            web = json.loads(by_name["search_web"].invoke({"query": "补充信息"}))
            assert web["result_count"] == 1
        result = json.loads(by_name["search_video_graph"].invoke({"query": "视频里发生了什么？"}))
        assert result["result_count"] > 0
        approved = json.loads(by_name["get_context_canvas"].invoke({"collection": "approved"}))
        drafts = []
        for item in approved["items"]:
            sections = _read_all(by_name["get_context_canvas"], item["reference"], collection="approved")
            drafts.append(sections["draft"])
        if self.merge_adjacent:
            drafts = [{**drafts[0], "end_seconds": drafts[-1]["end_seconds"]}]
        for draft in drafts:
            submitted = {
                "video_chunk_id": draft["video_chunk_id"],
                "start_seconds": draft["start_seconds"] + 1,
                "end_seconds": draft["end_seconds"] - 1,
                "claim": f"终审片段 {draft['video_chunk_id']} 支持最终回答。",
            }
            receipt = json.loads(by_name["submit_video_item"].invoke(submitted))
            assert receipt["accepted"]
            if self.revise and not self.submitted_items:
                handle_text("补充片段说明。")
                revised = {**submitted, "start_seconds": submitted["start_seconds"] + 1}
                assert json.loads(by_name["submit_video_item"].invoke(revised))["accepted"]
                assert json.loads(by_name["remove_video_item"].invoke({
                    "video_chunk_id": submitted["video_chunk_id"],
                }))["removed"]
                assert json.loads(by_name["submit_video_item"].invoke(submitted))["accepted"]
            self.submitted_items.append(submitted)
        handle_text(
            self.answer + " 网页补充 [来源](https://example.org/source)"
            if self.include_web else self.answer
        )


class _WebSearcherStub:
    def search(self, _query, *, max_results, timeout_seconds):
        assert max_results == 5
        assert timeout_seconds > 0
        return [WebSearchHit(
            title="网页来源", url="https://example.org/source", snippet="补充信息。",
        )]


def test_supervisor_chat_without_ready_video_series(build_service):
    model = _DirectSupervisorModel()
    service, conversation, _, repository, *_ = build_service(
        supervisor_model=model,
    )
    with repository._session_factory.begin() as session:
        session.query(Series).update({Series.status: "EMPTY"})

    completed = _send(service, conversation, content="你好")

    assert completed.user_message.series_ids == ()
    assert completed.assistant_message.series_ids == ()
    assert completed.assistant_message.content == "你好，我可以直接回答。"
    assert completed.assistant_message.search_result is None
    assert _agent_message_view(completed.assistant_message).series_ids == []
    prompt_context = json.loads(model.prompts[0].rsplit("\n\n", 1)[1])
    assert prompt_context["video_scope"] == []
    assert "没有可检索的视频系列" in prompt_context["video_scope_description"]


def test_direct_supervisor_streams_persisted_prefix_without_replaying_final_text(build_service):
    service, conversation, *_ = build_service(supervisor_model=_DirectSupervisorModel())
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default", content="你好",
        idempotency_key="direct-supervisor-stream", stream=True,
    )
    events = _collect(execution)
    completed = execution.result().assistant_message
    assert completed.status == "COMPLETED" and completed.search_result is None
    snapshots = [event["assistantMessage"] for event in events if event["type"] == "answer.snapshot"]
    assert snapshots and snapshots[-1]["content"] == completed.content
    assert all(message["status"] == "PROCESSING" for message in snapshots)
    assert not {"answer.evidence", "answer.block", "answer.delta"} & {event["type"] for event in events}
    assert events[-1]["type"] == "answer.completed"


def test_supervisor_prefix_is_readable_before_model_completion_and_survives_disconnect(build_service):
    entered, release = Event(), Event()

    class BlockingSupervisor(_DirectSupervisorModel):
        def run_agent(self, prompt, *, handle_text, check_control, **kwargs):
            super().run_agent(
                prompt, handle_text=handle_text, check_control=check_control, **kwargs,
            )
            entered.set()
            assert release.wait(5), "test did not release Supervisor"
            check_control()
            handle_text("接着解释后续内容。")

    service, conversation, *_ = build_service(supervisor_model=BlockingSupervisor())
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default", content="你好",
        idempotency_key="supervisor-prefix", stream=True,
    )
    try:
        assert entered.wait(5), "Supervisor did not publish its prefix"
        while (event := execution.next_event(timeout=2))["type"] != "answer.snapshot":
            pass
        persisted = service.get_message(
            conversation.conversation_id, tenant_id="local-default",
            message_id=execution.initial.assistant_message.message_id,
        ).assistant_message
        assert event["assistantMessage"] == asdict(persisted)
        assert persisted.content == "你好，我可以直接回答。"
        assert persisted.status == "PROCESSING" and not execution.future.done()
        execution.detach()
        assert not execution.cancellation.is_set()
    finally:
        release.set()
    completed = execution.result().assistant_message
    assert completed.status == "COMPLETED"
    assert "你好，我可以直接回答。" in completed.content
    assert "接着解释后续内容。" in completed.content
    assert completed == service.list_all_messages(
        conversation.conversation_id, tenant_id="local-default",
    )[-1]


@pytest.mark.parametrize("cancel", [False, True])
def test_inline_tool_snapshot_is_persisted_before_publish_and_survives_detach(
    build_service, monkeypatch, cancel,
):
    from server.service.agent_chat import AgentMessageExecution

    entered, release = Event(), Event()

    class BlockingSearch(_WebSearcherStub):
        def search(self, query, **kwargs):
            entered.set()
            assert release.wait(5)
            return super().search(query, **kwargs)

    class ToolSupervisor(_DirectSupervisorModel):
        def run_agent(self, _prompt, *, tools, handle_text, **_kwargs):
            handle_text("我先帮你检索演出资料。")
            next(tool for tool in tools if tool.name == "search_web").invoke({"query": "MyGO 演出"})
            handle_text("找到资料了，我来说明。")

    service, conversation, _, repository, *_ = build_service(
        supervisor_model=ToolSupervisor(), web_searcher=BlockingSearch(),
    )
    publish = AgentMessageExecution.publish

    def publish_after_checkpoint(execution, event):
        if event["type"] == "answer.snapshot":
            saved = service.get_message(
                conversation.conversation_id, tenant_id="local-default",
                message_id=execution.initial.assistant_message.message_id,
            ).assistant_message
            assert event["assistantMessage"] == asdict(saved)
        publish(execution, event)

    monkeypatch.setattr(AgentMessageExecution, "publish", publish_after_checkpoint)
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default", content="找演出",
        idempotency_key="inline-tool", stream=True,
    )
    try:
        assert entered.wait(5)
        pending = service.get_message(
            conversation.conversation_id, tenant_id="local-default",
            message_id=execution.initial.assistant_message.message_id,
        ).assistant_message
        assert [block["type"] for block in pending.render_blocks] == ["markdown", "tool"]
        tool = pending.render_blocks[1]
        assert tool["status"] == "inProgress" and tool["query"] == "MyGO 演出"
        execution.detach()
        if cancel:
            service.cancel_message(
                conversation.conversation_id, tenant_id="local-default", message_id=pending.message_id,
            )
    finally:
        release.set()
    if cancel:
        with pytest.raises(AgentModelInvocationError):
            execution.result()
    else:
        execution.result()
    restored = service.get_message(
        conversation.conversation_id, tenant_id="local-default", message_id=pending.message_id,
    ).assistant_message
    assert restored.render_blocks[1] == {**tool, "status": "failed" if cancel else "completed"}
    assert restored.version > pending.version
    assert restored.search_result is None
    assert restored.content == "\n\n".join(
        block["markdown"] for block in restored.render_blocks if block["type"] == "markdown"
    )
    if not cancel:
        assert [block["type"] for block in restored.render_blocks[:3]] == ["markdown", "tool", "markdown"]
    with repository._session_factory() as session:
        detail = session.get(AgentMessage, pending.message_id).detail
        assert detail["renderBlocks"] == list(restored.render_blocks)
        assert detail["timing"]["totalDurationMs"] is not None
        assert "补充信息。" not in json.dumps(detail, ensure_ascii=False)


@pytest.mark.parametrize("terminal", ["complete", "fail"])
def test_render_blocks_checkpoint_preserves_detail_and_closes_unfinished_tools(build_service, terminal):
    service, conversation, _, repository, *_ = build_service()
    pair = repository.start_message(
        conversation_id=conversation.conversation_id, tenant_id="local-default", content="测试",
        model_name="fixture", requested_top_k=1, requested_run_options={},
        idempotency_key="canonical-blocks", series_ids=[], requested_series_ids=[],
        user_message_id=str(uuid4()), assistant_message_id=str(uuid4()), now=1,
    )
    assistant_id = pair.assistant_message.message_id
    with repository._session_factory.begin() as session:
        record = session.get(AgentMessage, assistant_id)
        record.detail = {**record.detail, "request": {"custom": "kept"}}
        record.attachments = [{"mediaUri": "artifact://untouched"}]
    blocks = [
        {"type": "markdown", "markdown": "开场。"},
        {"type": "tool", "callId": "pending-call", "tool": "search_web", "status": "inProgress"},
        {"type": "markdown", "markdown": "下一段。"},
    ]
    checkpoint = repository.checkpoint_assistant_message(
        assistant_id, now=2, content="noncanonical text", render_blocks=blocks,
    )
    assert checkpoint.content == "开场。\n\n下一段。"
    assert checkpoint.version == 2
    repository.checkpoint_assistant_message(
        assistant_id, now=3, timing=AgentMessageTimingState(1, None, ()),
    )
    if terminal == "complete":
        restored = repository.complete_assistant_message(
            assistant_id, content=checkpoint.content, search_result=None, now=4,
            render_blocks=blocks,
        )
    else:
        restored = repository.fail_assistant_message(assistant_id, error="stopped", now=4)
    assert restored.render_blocks[1]["status"] == "failed"
    with repository._session_factory() as session:
        record = session.get(AgentMessage, assistant_id)
        assert record.detail["request"] == {"custom": "kept"}
        assert "timing" in record.detail
        assert record.attachments == [{"mediaUri": "artifact://untouched"}]
    with pytest.raises(AgentConflictError):
        repository.checkpoint_assistant_message(assistant_id, now=5, render_blocks=blocks)


@pytest.mark.parametrize("fault", ["start_snapshot", "end_snapshot", "phase", "all_checkpoints", "publish"])
def test_intermediate_delivery_failure_does_not_change_tool_result(build_service, monkeypatch, fault):
    from server.service.agent_chat import AgentMessageExecution

    class CountingSearch(_WebSearcherStub):
        def __init__(self):
            self.calls = []

        def search(self, query, **kwargs):
            self.calls.append(query)
            return super().search(query, **kwargs)

    class ToolSupervisor(_DirectSupervisorModel):
        def __init__(self):
            super().__init__()
            self.receipts = []

        def run_agent(self, _prompt, *, tools, handle_text, **_kwargs):
            handle_text("我先检索资料。")
            result = next(tool for tool in tools if tool.name == "search_web").invoke({"query": "MyGO 演出"})
            self.receipts.append(json.loads(result))
            handle_text("查到了，我来介绍。")

    model, search = ToolSupervisor(), CountingSearch()
    service, conversation, _, repository, *_ = build_service(supervisor_model=model, web_searcher=search)
    checkpoint, publish = repository.checkpoint_assistant_message, AgentMessageExecution.publish
    failures, published_snapshots = [], []

    def checkpoint_with_fault(message_id, **kwargs):
        tool = next((block for block in kwargs.get("render_blocks") or () if block["type"] == "tool"), None)
        matched = (
            fault == "all_checkpoints"
            or fault == "phase" and kwargs.get("phase") == AnswerPhase.RETRIEVING
            or fault == "start_snapshot" and tool is not None and tool["status"] == "inProgress"
            or fault == "end_snapshot" and tool is not None and tool["status"] == "completed"
        )
        if matched and (not failures or fault == "all_checkpoints"):
            failures.append("checkpoint")
            raise RuntimeError("injected UI checkpoint failure")
        return checkpoint(message_id, **kwargs)

    def publish_with_fault(execution, event):
        if event["type"] == "answer.snapshot":
            saved = service.get_message(
                conversation.conversation_id, tenant_id="local-default",
                message_id=execution.initial.assistant_message.message_id,
            ).assistant_message
            published_snapshots.append((event["assistantMessage"], asdict(saved)))
            if fault == "publish" and not failures and any(
                block["type"] == "tool" for block in saved.render_blocks
            ):
                failures.append("publish")
                raise RuntimeError("injected UI publication failure")
        publish(execution, event)

    monkeypatch.setattr(repository, "checkpoint_assistant_message", checkpoint_with_fault)
    monkeypatch.setattr(AgentMessageExecution, "publish", publish_with_fault)
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default", content="找演出资料",
        idempotency_key="observer-fault", stream=True,
    )
    events = _collect(execution)
    completed = execution.result().assistant_message
    restored = service.get_message(
        conversation.conversation_id, tenant_id="local-default", message_id=completed.message_id,
    ).assistant_message
    assert failures and (fault == "all_checkpoints" or len(failures) == 1)
    assert search.calls == ["MyGO 演出"]
    assert model.receipts == [{
        "items": [{"title": "网页来源", "url": "https://example.org/source", "snippet": "补充信息。"}],
        "result_count": 1, "omitted_count": 0,
    }]
    assert restored == completed and completed.status == "COMPLETED"
    assert [block["type"] for block in completed.render_blocks[:3]] == ["markdown", "tool", "markdown"]
    assert completed.render_blocks[0]["markdown"] == "我先检索资料。"
    assert completed.render_blocks[2]["markdown"] == "查到了，我来介绍。"
    tools = [block for block in completed.render_blocks if block["type"] == "tool"]
    assert len(tools) == 1 and tools[0]["status"] == "completed" and tools[0]["callId"]
    assert all(sent == saved for sent, saved in published_snapshots)
    if fault == "all_checkpoints":
        assert published_snapshots == []
    assert events[-1]["type"] == "answer.completed"
    assert not {"answer.failed", "answer.interrupted"} & {event["type"] for event in events}


@pytest.mark.parametrize("checkpoint_error", [AgentConflictError, AgentMessageNotFoundError])
def test_inactive_message_checkpoint_stops_tool_execution(build_service, monkeypatch, checkpoint_error):
    searched, continued, failures = [], [], []

    class Search(_WebSearcherStub):
        def search(self, query, **kwargs):
            searched.append(query)
            return super().search(query, **kwargs)

    class ToolSupervisor(_DirectSupervisorModel):
        def run_agent(self, _prompt, *, tools, handle_text, **_kwargs):
            handle_text("我先检索资料。")
            next(tool for tool in tools if tool.name == "search_web").invoke({"query": "MyGO 演出"})
            continued.append(True)
            handle_text("查到了，我来介绍。")

    service, conversation, _, repository, *_ = build_service(
        supervisor_model=ToolSupervisor(), web_searcher=Search(),
    )
    checkpoint = repository.checkpoint_assistant_message

    def checkpoint_inactive(message_id, **kwargs):
        if not failures and any(
            block["type"] == "tool" and block["status"] == "inProgress"
            for block in kwargs.get("render_blocks") or ()
        ):
            failures.append(checkpoint_error)
            raise checkpoint_error("message no longer active")
        return checkpoint(message_id, **kwargs)

    monkeypatch.setattr(repository, "checkpoint_assistant_message", checkpoint_inactive)
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default", content="找演出资料",
        idempotency_key="inactive-message", stream=True,
    )
    events = _collect(execution)
    with pytest.raises(AgentModelInvocationError):
        execution.result()
    assert failures == [checkpoint_error] and execution.cancellation.is_set()
    assert searched == [] and continued == []
    assert not any(event["type"] == "answer.completed" for event in events)
    failed = service.get_message(
        conversation.conversation_id, tenant_id="local-default",
        message_id=execution.initial.assistant_message.message_id,
    ).assistant_message
    assert failed.status == "FAILED" and failed.content == "我先检索资料。"


@pytest.mark.parametrize("playback_change", [
    None, "position", "series", "tenant", "negative_position", "float_position", "bool_position",
])
def test_current_video_scope_is_trusted_without_making_unready_series_retrievable(
    build_service, playback_change,
):
    background = {"format": "markdown", "content": "若叶睦是蓝发双马尾的乐队成员。" * 900}

    class CurrentVideoModel(_DirectSupervisorModel):
        model_name = "current-video-test"
        supports_vision_input = False

        def run_agent(self, _prompt, *, tools, handle_text, **_kwargs):
            context = json.loads(_prompt.rsplit("\n\n", 1)[1])
            assert context["video_scope"] == []
            assert len(context["series_context"]) == 1
            current = context["series_context"][0]
            assert current["seriesId"] == series_id
            assert current["description"] == "乐队人物资料"
            assert current["transcriptLanguageHint"] == "ja"
            assert current["importantExtraData"] == background
            assert current["characterBible"] == [{"name": "若叶睦", "aliases": ["睦"]}]
            by_name = {tool.name: tool for tool in tools}
            graph = json.loads(by_name["search_video_graph"].invoke({"query": "now"}))
            assert graph["error"]["code"] == "NO_VIDEO_SCOPE"
            handle_text("当前视频答疑可使用实时理解。")

    service, conversation, _, repository, *_ = build_service(
        supervisor_model=CurrentVideoModel(), realtime_runtime=object(),
    )
    video_id = str(uuid4())
    with repository._session_factory.begin() as session:
        series = session.query(Series).one()
        series.status = "EMPTY"
        series.description = "乐队人物资料"
        series.transcript_language_hint = "ja"
        series.important_extra_data = background
        series.character_bible = [{"name": "若叶睦", "aliases": ["睦"]}]
        series_id = series.id
        if playback_change == "tenant":
            series.tenant_id = "other-tenant"
        session.add(Video(
            id=video_id, series_id=series_id, episode_order=1, upload_sequence=1,
            filename="lecture.mp4", source_uri="source/lecture.mp4",
            hash="a" * 64, size_bytes=1, duration_ms=300_000,
            upload_status="UPLOADED", parse_status="PENDING",
            upload_progress=100, parse_progress=0,
        ))
    playback = {
        "seriesId": series_id, "videoId": video_id, "episodeOrder": 1,
        "positionSeconds": 123_456,
    }
    if playback_change == "position":
        playback["positionSeconds"] = 300_001
    elif playback_change == "negative_position":
        playback["positionSeconds"] = -1
    elif playback_change == "float_position":
        playback["positionSeconds"] = 123_456.0
    elif playback_change == "bool_position":
        playback["positionSeconds"] = True
    elif playback_change == "series":
        playback["seriesId"] = str(uuid4())
    if playback_change is not None:
        with pytest.raises(SeriesNotFoundError if playback_change == "tenant" else ValueError):
            _send(service, conversation, series_ids=[series_id], extra_context={"playback": playback})
        assert service.list_all_messages(conversation.conversation_id, tenant_id="local-default") == []
        return
    completed = _send(
        service, conversation, series_ids=[series_id], extra_context={"playback": playback},
    )
    assert completed.user_message.series_ids == ()
    assert completed.user_message.extra_context["playback"]["positionSeconds"] == 123_456
    assert completed.assistant_message.content == "当前视频答疑可使用实时理解。"
    with pytest.raises(SeriesNotReadyError):
        _send(service, conversation, key="without-playback", series_ids=[series_id])


@pytest.mark.parametrize("include_other_unready", [False, True])
def test_current_unready_video_exemption_keeps_other_retrieval_scope_rules(
    build_service, include_other_unready,
):
    class CurrentVideoModel(_DirectSupervisorModel):
        model_name = "current-video-test"

        def run_agent(self, _prompt, *, tools, handle_text, **_kwargs):
            context = json.loads(_prompt.rsplit("\n\n", 1)[1])
            assert [item["seriesId"] for item in context["video_scope"]] == [ready.series_id]
            assert {item["seriesId"] for item in context["series_context"]} == {current_id, ready.series_id}
            by_name = {tool.name: tool for tool in tools}
            by_name["search_video_graph"].invoke({"query": "what was explained"})
            handle_text("Answer from the available evidence.")

    service, conversation, retrieval, repository, query, *_ = build_service(
        supervisor_model=CurrentVideoModel(), realtime_runtime=object(),
    )
    ready = service._library.create_series(tenant_id="local-default", name="Ready course")
    other = service._library.create_series(tenant_id="local-default", name="Other pending course")
    video_id = str(uuid4())
    with repository._session_factory.begin() as session:
        current = session.query(Series).filter(Series.name == "Test Series").one()
        current.status = "EMPTY"
        current_id = current.id
        session.get(Series, ready.series_id).status = "READY"
        session.add(Video(
            id=video_id, series_id=current_id, episode_order=1, upload_sequence=1,
            filename="lecture.mp4", source_uri="source/lecture.mp4",
            hash="a" * 64, size_bytes=1, duration_ms=300_000,
            upload_status="UPLOADED", parse_status="PENDING",
            upload_progress=100, parse_progress=0,
        ))
    scope = [current_id, ready.series_id]
    if include_other_unready:
        scope.append(other.series_id)
    kwargs = dict(series_ids=scope, extra_context={"playback": {
        "seriesId": current_id, "videoId": video_id, "episodeOrder": 1,
        "positionSeconds": 123_456,
    }})
    if include_other_unready:
        with pytest.raises(SeriesNotReadyError):
            _send(service, conversation, **kwargs)
        assert retrieval.calls == []
        assert service.list_all_messages(conversation.conversation_id, tenant_id="local-default") == []
    else:
        result = _send(service, conversation, **kwargs)
        assert result.user_message.series_ids == (ready.series_id,)
        assert retrieval.calls and all(call[1] == [ready.series_id] for call in retrieval.calls)
        query_context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])["inputs"]
        assert [item["seriesId"] for item in query_context["series_context"]] == [ready.series_id]


@pytest.mark.parametrize("with_playback", [False, True])
def test_supervisor_and_video_search_share_one_background_snapshot_per_turn(
    build_service, monkeypatch, with_playback,
):
    first_background = {"format": "markdown", "content": "人物原名与别名。"}
    next_background = {"format": "markdown", "content": "更新后的人物介绍。"}
    reads = []

    class SnapshotModel(_DirectSupervisorModel):
        def run_agent(self, prompt, *, tools, handle_text, **_kwargs):
            self.prompts.append(prompt)
            context = json.loads(prompt.rsplit("\n\n", 1)[1])
            expected = first_background if len(self.prompts) == 1 else next_background
            assert [item["seriesId"] for item in context["video_scope"]] == [series_id]
            assert len(context["series_context"]) == 1
            assert context["series_context"][0]["importantExtraData"] == expected
            if len(self.prompts) == 1:
                with repository._session_factory.begin() as session:
                    session.get(Series, series_id).important_extra_data = next_background
            reads_before_tool = len(reads)
            next(tool for tool in tools if tool.name == "search_video_graph").invoke({"query": "核验人物"})
            assert len(reads) == reads_before_tool
            query_context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])["inputs"]
            assert len(query_context["series_context"]) == 1
            assert query_context["series_context"][0]["importantExtraData"] == expected
            handle_text("Answer from the available evidence.")

    model = SnapshotModel()
    service, conversation, _, repository, query, *_ = build_service(
        supervisor_model=model, realtime_runtime=object() if with_playback else None,
    )
    video_id = str(uuid4())
    with repository._session_factory.begin() as session:
        series = session.query(Series).one()
        series_id = series.id
        series.important_extra_data = first_background
        if with_playback:
            session.add(Video(
                id=video_id, series_id=series_id, episode_order=1, upload_sequence=1,
                filename="lecture.mp4", source_uri="source/lecture.mp4",
                hash="a" * 64, size_bytes=1, duration_ms=300_000,
                upload_status="UPLOADED", parse_status="PENDING",
                upload_progress=100, parse_progress=0,
            ))
    get_series = service._library.get_series

    def record_read(*args, **kwargs):
        reads.append(args[0])
        return get_series(*args, **kwargs)

    monkeypatch.setattr(service._library, "get_series", record_read)
    extra_context = {"playback": {
        "seriesId": series_id, "videoId": video_id, "episodeOrder": 1,
        "positionSeconds": 123_456,
    }} if with_playback else None
    first = _send(service, conversation, series_ids=[series_id], extra_context=extra_context)
    reads_after_first = len(reads)
    assert _send(service, conversation, series_ids=[series_id], extra_context=extra_context) == first
    assert len(reads) == reads_after_first
    assert len(model.prompts) == 1
    second = _send(
        service, conversation, key="updated-background", series_ids=[series_id],
        extra_context=extra_context,
    )
    assert first.assistant_message.status == second.assistant_message.status == "COMPLETED"
    assert len(model.prompts) == 2


def test_extra_context_is_persisted_injected_and_part_of_idempotency(build_service):
    model = _DirectSupervisorModel()
    service, conversation, _, _, *_ = build_service(supervisor_model=model)
    extra_context = {
        "playback": {
            "seriesId": "series-1",
            "videoId": "video-1",
            "episodeOrder": 1,
            "positionSeconds": 122,
        },
    }

    completed = _send(
        service,
        conversation,
        content="这里在讲什么？",
        extra_context=extra_context,
    )

    assert completed.user_message.extra_context == extra_context
    assert completed.assistant_message.extra_context is None
    prompt_context = json.loads(model.prompts[0].rsplit("\n\n", 1)[1])
    assert prompt_context["extra_context"] == extra_context
    assert len(prompt_context["series_context"]) == 1
    assert prompt_context["series_context"][0]["seriesId"] != extra_context["playback"]["seriesId"]
    assert _send(
        service,
        conversation,
        content="这里在讲什么？",
        extra_context=extra_context,
    ) == completed
    with pytest.raises(AgentConflictError, match="different request fields"):
        _send(
            service,
            conversation,
            content="这里在讲什么？",
            extra_context={"playback": {**extra_context["playback"], "positionSeconds": 123}},
        )


def test_attachment_references_roundtrip_and_order_participates_in_idempotency(build_service):
    service, conversation, _, repository, *_ = build_service()
    references = [{"mediaUri": "artifact://test/first"}, {"mediaUri": "artifact://test/second"}]
    request = dict(
        conversation_id=conversation.conversation_id, tenant_id="local-default",
        content="这个表情包出自哪里？", model_name="fixture", requested_top_k=5,
        requested_run_options={}, idempotency_key="attachment-pair",
        series_ids=[], requested_series_ids=[], user_message_id=str(uuid4()),
        assistant_message_id=str(uuid4()), now=1_700_000_000_000,
        attachments=references,
    )
    pair = repository.start_message(**request)
    assert pair.user_message.attachments == tuple(references)
    assert pair.assistant_message.attachments == ()
    recovered = repository.get_message_pair(
        conversation.conversation_id, "local-default", idempotency_key="attachment-pair",
    )
    assert recovered.user_message.attachments == tuple(references)
    assert repository.start_message(**request).reused
    for changed in ([], references[:1], references[::-1]):
        with pytest.raises(AgentConflictError):
            repository.start_message(**{**request, "attachments": changed})
    with repository._session_factory() as session:
        user = session.get(AgentMessage, pair.user_message.message_id)
        assert user.attachments == references
        assert "attachments" not in user.detail
        assert session.scalar(select(func.count()).select_from(AgentMessage)) == 2


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("supervised", [False, True])
def test_uploaded_attachment_reaches_runtime_without_service_vision_gate(
    build_service, tmp_path, monkeypatch, stream, supervised,
):
    import base64
    from io import BytesIO
    from PIL import Image
    from server.config.media import ImageUploadConfig
    from server.service.agent_attachments import AgentAttachmentService
    from server.storage.artifact import LocalArtifactStore

    model = _DirectSupervisorModel() if supervised else None
    service, conversation, retrieval, repository, query, *_ = build_service(supervisor_model=model)
    observed = []
    kick = service._runtime.kick

    def capture(inputs, **kwargs):
        observed.append(inputs)
        return kick(inputs, **kwargs)

    monkeypatch.setattr(service._runtime, "kick", capture)
    attachments = AgentAttachmentService(
        repository, LocalArtifactStore(tmp_path / "media", environment="test"),
        config=ImageUploadConfig(),
    )
    service._attachments = attachments
    image = BytesIO()
    Image.new("RGB", (10, 10), "red").save(image, format="PNG")
    uploaded = attachments.upload(
        conversation.conversation_id, image.getvalue(), tenant_id="local-default",
        owner_user_id=conversation.owner_user_id,
    )
    call = service.start_message if stream else service.send_message
    result = call(
        conversation.conversation_id, tenant_id="local-default", content="这张图出自哪里？",
        idempotency_key="image-question", attachments=[{"mediaUri": uploaded.media_uri}],
        **({"stream": True} if stream else {}),
    )
    if stream:
        assert _collect(result)[-1]["type"] == "answer.completed"
        result = result.result()
    assert result.assistant_message.status == "COMPLETED"
    assert len(observed) == 1
    assert [base64.b64decode(url.split(",", 1)[1]) for url in observed[0]["image_urls"]] == [image.getvalue()]
    assert len(service.list_all_messages(conversation.conversation_id, tenant_id="local-default")) == 2
    assert bool(query.prompts) is not supervised
    assert model is None or len(model.turns) == 1
    assert attachments.get_content(
        conversation.conversation_id, uploaded.media_uri, tenant_id="local-default",
        owner_user_id=conversation.owner_user_id,
    ).path.read_bytes() == image.getvalue()
    assert _send(service, conversation).assistant_message.status == "COMPLETED"


def test_vision_supervisor_receives_uploaded_bytes_in_order_and_reloads_attachment_history(build_service, tmp_path):
    import base64
    from io import BytesIO
    from PIL import Image
    from server.config.media import ImageUploadConfig
    from server.service.agent_attachments import AgentAttachmentService
    from server.storage.artifact import LocalArtifactStore

    class VisionModel(_DirectSupervisorModel):
        supports_vision_input = True

        def __init__(self):
            super().__init__()
            self.image_inputs = []
            self.session_histories = []

        def run_agent(self, prompt, **kwargs):
            self.image_inputs.append(kwargs.get("image_urls"))
            self.session_histories.append(kwargs["session_context"].load_history(-1))
            return super().run_agent(prompt, **kwargs)

    model = VisionModel()
    service, _, retrieval, repository, *_ = build_service(supervisor_model=model)
    conversation = service.create_conversation(
        tenant_id="private-chat", owner_user_id="image-owner", idempotency_key="private-images",
    )
    scope = {"tenant_id": "private-chat", "owner_user_id": "image-owner"}
    attachments = AgentAttachmentService(
        repository, LocalArtifactStore(tmp_path / "images", environment="test"), config=ImageUploadConfig(),
    )
    service._attachments = attachments
    originals, references = [], []
    for color in ("red", "green", "blue", "yellow", "purple", "orange"):
        image = BytesIO()
        Image.new("RGB", (10, 10), color).save(image, format="PNG")
        originals.append(image.getvalue())
        uploaded = attachments.upload(conversation.conversation_id, originals[-1], **scope)
        references.append({"mediaUri": uploaded.media_uri})
    order = [3, 0, 5, 1, 4, 2]
    references = [references[index] for index in order]
    expected_bytes = [originals[index] for index in order]
    request = {**scope, "source_tenant_id": "local-default", "content": "这些图片有什么不同？",
               "idempotency_key": "six-images", "attachments": references}
    first = service.send_message(conversation.conversation_id, **request)
    assert first.assistant_message.status == "COMPLETED"
    assert first.user_message.attachments == tuple(references)
    assert first.assistant_message.source_tenant_id == "local-default"
    assert len(model.image_inputs[0]) == 6
    assert all(url.startswith("data:image/png;base64,") for url in model.image_inputs[0])
    assert [base64.b64decode(url.split(",", 1)[1]) for url in model.image_inputs[0]] == expected_bytes
    assert model.turns[0][3] == []
    assert service.send_message(conversation.conversation_id, **request) == first
    assert len(model.turns) == 1

    second = service.send_message(
        conversation.conversation_id, **scope, source_tenant_id="local-default",
        content="再比较一下刚才的图片。", idempotency_key="image-follow-up",
    )
    assert second.assistant_message.status == "COMPLETED"
    assert model.image_inputs[1] is None
    history = model.turns[1][3]
    assert [item["role"] for item in history] == ["USER", "ASSISTANT"]
    assert [base64.b64decode(url.split(",", 1)[1]) for url in history[0]["imageUrls"]] == expected_bytes
    assert "imageUrls" not in history[1]
    session_history = model.session_histories[1]
    assert model.session_histories[0] == []
    assert [(item["messageId"], item["sequenceNo"]) for item in session_history] == [
        (first.user_message.message_id, first.user_message.sequence_no),
        (first.assistant_message.message_id, first.assistant_message.sequence_no),
    ]
    assert [base64.b64decode(url.split(",", 1)[1]) for url in session_history[0]["imageUrls"]] == expected_bytes
    assert "imageUrls" not in session_history[1]
    assert service.send_message(conversation.conversation_id, **request) == first
    assert len(model.turns) == 2 and not retrieval.calls
    assert all("data:image" not in prompt for prompt in model.prompts)
    with repository._session_factory() as session:
        rows = session.execute(select(AgentMessage.__table__).where(
            AgentMessage.conversation_id == conversation.conversation_id,
        )).mappings().all()
        user = next(row for row in rows if row["id"] == first.user_message.message_id)
        assert user["attachments"] == references
        persisted = json.dumps([dict(row) for row in rows], default=str)
        assert "data:image" not in persisted and "base64" not in persisted
        assert all(url.split(",", 1)[1] not in persisted for url in model.image_inputs[0])


def test_vision_attachment_input_still_rejects_invalid_and_cross_conversation_references(build_service, tmp_path):
    from io import BytesIO
    from PIL import Image
    from server.config.media import ImageUploadConfig
    from server.service.agent_attachments import AgentAttachmentService
    from server.storage.artifact import LocalArtifactStore

    class VisionModel(_DirectSupervisorModel):
        supports_vision_input = True

    model = VisionModel()
    service, conversation, _, repository, *_ = build_service(supervisor_model=model)
    attachments = AgentAttachmentService(
        repository, LocalArtifactStore(tmp_path / "images", environment="test"), config=ImageUploadConfig(),
    )
    service._attachments = attachments
    other = service.create_conversation(tenant_id="local-default", idempotency_key="other-images")
    image = BytesIO()
    Image.new("RGB", (10, 10), "red").save(image, format="PNG")
    uploaded = attachments.upload(other.conversation_id, image.getvalue(), tenant_id="local-default")
    for media_uri in ("file:///tmp/unmanaged.png", "data:image/png;base64,unmanaged", uploaded.media_uri):
        with pytest.raises(ValueError, match="does not belong to this conversation"):
            _send(service, conversation, attachments=[{"mediaUri": media_uri}])
    with pytest.raises(AgentConversationNotFoundError):
        service.send_message(
            conversation.conversation_id, tenant_id="other-tenant", content="图片问题",
            idempotency_key="wrong-tenant", attachments=[{"mediaUri": uploaded.media_uri}],
        )
    assert not model.turns and not service._executions
    assert service.list_all_messages(conversation.conversation_id, tenant_id="local-default") == []


def test_attachment_history_without_storage_blocks_new_inference_but_terminal_replay_recovers(build_service):
    service, conversation, retrieval, repository, *_ = build_service()
    completed = _send(service, conversation)
    references = [{"mediaUri": "artifact://test/historical-image"}]
    with repository._session_factory.begin() as session:
        session.get(AgentMessage, completed.user_message.message_id).attachments = references
    with pytest.raises(AgentAttachmentInputUnavailableError, match="附件存储服务尚未配置"):
        _send(service, conversation, key="follow-up", content="刚才那张图呢？")
    recovered = _send(service, conversation, attachments=references)
    assert recovered.user_message.attachments == tuple(references)
    assert len(retrieval.calls) == 1
    assert len(service.list_all_messages(conversation.conversation_id, tenant_id="local-default")) == 2


def test_legacy_null_attachments_and_empty_request_have_same_identity(build_service):
    service, conversation, _, repository, *_ = build_service()
    first = _send(service, conversation)
    with repository._session_factory.begin() as session:
        session.get(AgentMessage, first.user_message.message_id).attachments = None
    replay = _send(service, conversation, attachments=[])
    assert replay.user_message.attachments == replay.assistant_message.attachments == ()
    assert replay.user_message.message_id == first.user_message.message_id


@pytest.mark.parametrize("failure", [OSError("storage offline"), RuntimeError("cleanup DB unavailable")])
def test_attachment_cleanup_failure_does_not_undo_committed_conversation_delete(build_service, caplog, failure):
    model = _DirectSupervisorModel()
    service, conversation, _, repository, *_ = build_service(
        supervisor_model=model, enforce_foreign_keys=True,
    )

    class FailingCleanup:
        def delete_conversation_artifacts(self, conversation_id):
            assert not repository.conversation_exists(conversation_id)
            raise failure

    service._attachments = FailingCleanup()
    with caplog.at_level(logging.WARNING):
        deleted = service.delete_conversation(conversation.conversation_id, tenant_id="local-default")
    assert deleted.conversation_id == conversation.conversation_id
    assert not repository.conversation_exists(conversation.conversation_id)
    assert model.deleted == [conversation.conversation_id]
    assert "Could not delete conversation attachments" in caplog.text


@pytest.mark.parametrize("include_web", [False, True])
@pytest.mark.parametrize("commit_failure", [False, True])
@pytest.mark.parametrize("merge_adjacent", [False, True])
def test_supervisor_final_review_persists_and_streams_its_own_text_and_ranges(
    build_service, prepared_import, monkeypatch, include_web, commit_failure, merge_adjacent,
):
    from server.service.agent_chat import AgentMessageExecution

    model = _VideoSupervisorModel(include_web=include_web, merge_adjacent=merge_adjacent, revise=True)
    service, conversation, retrieval, repository, query, *_ = build_service(
        supervisor_model=model,
        web_searcher=_WebSearcherStub(),
    )
    videos = VideoRepository(repository._session_factory)
    report = videos.import_video_parse(prepared_import)
    with repository._session_factory.begin() as session:
        session.get(Series, report.series_id).status = "READY"
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
        (chunk.video_chunk_id, chunk.start_seconds, chunk.end_seconds)
        for chunk in chunks
    )

    publish = AgentMessageExecution.publish

    def publish_after_checkpoint(execution, answer_event):
        if answer_event["type"] == "answer.snapshot":
            saved = service.get_message(
                conversation.conversation_id, tenant_id="local-default",
                message_id=execution.initial.assistant_message.message_id,
            ).assistant_message
            assert saved.status == "PROCESSING"
            assert answer_event["assistantMessage"] == asdict(saved)
            assert all(message_id != saved.message_id for _, message_id, _ in model.confirmed)
        publish(execution, answer_event)

    monkeypatch.setattr(AgentMessageExecution, "publish", publish_after_checkpoint)
    if commit_failure:
        def fail_commit(*_args, **_kwargs):
            raise RuntimeError("injected final review commit failure")
        monkeypatch.setattr(repository, "complete_assistant_message", fail_commit)
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default",
        content="视频里发生了什么？", idempotency_key="supervisor-final", stream=True,
    )
    events = _collect(execution)
    if commit_failure:
        with pytest.raises(RuntimeError, match="final review commit failure"):
            execution.result()
        failed = service.get_message(
            conversation.conversation_id, tenant_id="local-default",
            message_id=execution.initial.assistant_message.message_id,
        ).assistant_message
        assert failed.status == "FAILED" and model.answer in failed.content
        assert failed.search_result is not None
        assert model.submitted_items and not model.confirmed
        assert events[-1]["type"] == "answer.failed"
        assert not {"answer.completed", "answer.evidence", "answer.block", "answer.delta"} & {
            event["type"] for event in events
        }
        snapshots = [event for event in events if event["type"] == "answer.snapshot"]
        assert snapshots[-1]["assistantMessage"]["content"] == failed.content
        assert snapshots[-1]["assistantMessage"]["search_result"] == failed.search_result
        assert events[-1]["assistantMessage"] == asdict(failed)
        return
    completed = execution.result()

    assert completed.assistant_message.search_result is not None
    assert completed.assistant_message.content == (
        completed.assistant_message.search_result["agentReply"]["message"]
    )
    assert model.answer in completed.assistant_message.content
    assert "目前没有足够的候选画面" not in completed.assistant_message.content
    assert model.confirmed == [(
        conversation.conversation_id,
        completed.assistant_message.message_id,
        completed.assistant_message.content,
    )]
    result = completed.assistant_message.search_result
    assert "renderBlocks" not in result
    rendered = completed.assistant_message.render_blocks
    video_blocks = [block for block in rendered if block["type"] == "video"]
    public_video_blocks = [{
        "type": "video", "videoChunkId": item["video_chunk_id"],
        "startSeconds": item["start_seconds"], "endSeconds": item["end_seconds"],
    } for item in model.submitted_items]
    source_ids = [chunk.video_chunk_id for chunk in chunks]
    assert video_blocks == [
        {**block, **({"sourceVideoChunkIds": source_ids} if merge_adjacent else {})}
        for block in public_video_blocks
    ]
    assert [(block["startSeconds"], block["endSeconds"]) for block in video_blocks] == [
        (start + 1, end - 1) for start, end in (
            [(chunks[0].start_seconds, chunks[-1].end_seconds)] if merge_adjacent
            else [(start, end) for _identifier, start, end in query.selected_ranges]
        )
    ]
    hits_by_id = {hit["candidate"]["videoChunkId"]: hit for hit in result["hits"]}
    assert set(hits_by_id) == set(source_ids)
    for item in model.submitted_items:
        assert hits_by_id[item["video_chunk_id"]]["selectionReason"]["summary"] == item["claim"]
    snapshots = [event["assistantMessage"] for event in events if event["type"] == "answer.snapshot"]
    assert snapshots[-1]["content"] == completed.assistant_message.content
    assert snapshots[-1]["search_result"] == result
    assert any(block["type"] == "video" for message in snapshots for block in message["render_blocks"])
    anchor = model.submitted_items[0]["video_chunk_id"]
    anchor_versions = [
        next((block for block in message["render_blocks"]
              if block["type"] == "video" and block["videoChunkId"] == anchor), None)
        for message in snapshots
    ]
    anchor_versions = anchor_versions[next(index for index, block in enumerate(anchor_versions) if block is not None):]
    assert anchor_versions[0]["startSeconds"] == model.submitted_items[0]["start_seconds"]
    assert any(block is not None and block["startSeconds"] == anchor_versions[0]["startSeconds"] + 1
               for block in anchor_versions)
    assert None in anchor_versions and anchor_versions[-1] == anchor_versions[0]
    assert not {"answer.evidence", "answer.block", "answer.delta"} & {event["type"] for event in events}
    assert events[-1]["type"] == "answer.completed"
    if include_web:
        assert rendered[-1]["type"] == "markdown"
        assert "https://example.org/source" in rendered[-1]["markdown"]
    if not include_web:
        source_id = completed.assistant_message.message_id
        reference = {"messageId": source_id, "videoChunkId": video_blocks[0]["videoChunkId"]}
        _send(service, conversation, key="supervisor-reference", reference=reference)
        context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])["inputs"]
        assert context["reference_context"]["selectedRange"] == {
            "startSeconds": video_blocks[0]["startSeconds"],
            "endSeconds": video_blocks[0]["endSeconds"],
        }
        if merge_adjacent:
            supporting = context["reference_context"]["supportingEvidence"]
            assert [item["videoChunkId"] for item in supporting] == source_ids[1:]
            assert supporting[0]["windowSummary"] == hits_by_id[source_ids[1]]["evidence"]["windowSummary"]
        assert context["history"][1]["content"] == completed.assistant_message.content
        assert context["history"][1]["searchResult"]["videoRanges"] == [{
            key: block[key] for key in ("videoChunkId", "startSeconds", "endSeconds", "sourceVideoChunkIds")
            if key in block
        } for block in video_blocks]
        assert model.turns[-1][3][1]["content"] == completed.assistant_message.content


def test_supervisor_confirms_only_after_sql_commit_and_receives_full_history(
    build_service, monkeypatch,
):
    model = _DirectSupervisorModel()
    service, conversation, _, repository, *_ = build_service(supervisor_model=model)
    confirm = model.confirm_turn

    def confirm_saved(thread_id, message_id, content):
        pair = repository.get_message_pair(
            thread_id, "local-default", message_id=message_id,
        )
        assert pair.assistant_message.status == "COMPLETED"
        assert pair.assistant_message.content == content
        confirm(thread_id, message_id, content)

    monkeypatch.setattr(model, "confirm_turn", confirm_saved)
    question = "完整历史内容" * 1_000
    first = _send(service, conversation, content=question)
    assert _send(service, conversation, content=question) == first
    second = _send(service, conversation, key="second", content="继续")

    assert model.confirmed == [
        (conversation.conversation_id, first.assistant_message.message_id, first.assistant_message.content),
        (conversation.conversation_id, second.assistant_message.message_id, second.assistant_message.content),
    ]
    thread_id, message_id, user_message, history = model.turns[-1]
    assert (thread_id, message_id, user_message) == (
        conversation.conversation_id, second.assistant_message.message_id, "继续",
    )
    assert history[0]["content"] == question
    assert history[1]["content"] == first.assistant_message.content
    assert model.discarded == []


@pytest.mark.parametrize("error_type", [RuntimeError, AgentConversationNotFoundError])
def test_supervisor_session_state_read_failure_preserves_binding_and_lifecycle_errors(
    build_service, monkeypatch, caplog, error_type,
):
    class ContextModel(_DirectSupervisorModel):
        def __init__(self):
            super().__init__()
            self.contexts = []

        def run_agent(self, prompt, **kwargs):
            self.contexts.append(kwargs.get("session_context"))
            return super().run_agent(prompt, **kwargs)

    model = ContextModel()
    service, conversation, _, repository, *_ = build_service(supervisor_model=model)
    original_detail = {"custom": {"label": "保留"}, "runtimeSessions": {"codex-A": {
        "threadId": "original-native-thread", "syncedThroughSequence": -1,
    }}}
    with repository._session_factory.begin() as session:
        session.get(AgentConversation, conversation.conversation_id).detail = original_detail
    get_conversation = repository.get_conversation
    reads = []

    def fail_metadata_read(*args, **kwargs):
        reads.append((args, kwargs))
        raise error_type("injected session metadata read failure")

    monkeypatch.setattr(repository, "get_conversation", fail_metadata_read)
    if error_type is AgentConversationNotFoundError:
        with pytest.raises(AgentConversationNotFoundError, match="session metadata"):
            _send(service, conversation)
        assert model.contexts == [] and not model.confirmed
    else:
        result = _send(service, conversation)
        assert result.assistant_message.status == "COMPLETED"
        assert result.assistant_message.content == "你好，我可以直接回答。"
        assert model.contexts == [None] and len(model.confirmed) == 1
        assert "Could not load Supervisor session state" in caplog.text
    assert len(reads) == 1
    assert get_conversation(conversation.conversation_id, "local-default").detail == original_detail


@pytest.mark.parametrize("error_type", [
    AgentConflictError, AgentMessageNotFoundError, AgentConversationNotFoundError, ConnectionError,
])
def test_session_state_save_marks_lifecycle_failure_but_keeps_storage_failure_optional(
    build_service, monkeypatch, error_type,
):
    class SavingModel(_DirectSupervisorModel):
        def run_agent(self, prompt, *, session_context, **kwargs):
            try:
                session_context.save_state("codex-A", {"threadId": "native-thread"})
            except Exception as exc:
                self.save_error = exc
            kwargs["check_control"]()
            return super().run_agent(prompt, **kwargs)

    model = SavingModel()
    service, conversation, _, repository, *_ = build_service(supervisor_model=model)
    save_error = error_type("injected session save failure")

    def fail_save(*args, **kwargs):
        raise save_error

    monkeypatch.setattr(repository, "save_runtime_session", fail_save)
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default",
        content="继续回答当前问题", idempotency_key="session-save-failure",
    )
    if error_type is ConnectionError:
        result = execution.result()
        assert result.assistant_message.status == "COMPLETED"
        assert result.assistant_message.content == "你好，我可以直接回答。"
        assert not execution.cancellation.is_set()
    else:
        with pytest.raises(AgentModelInvocationError, match="cancelled"):
            execution.result()
        assert execution.cancellation.is_set()
        assert not model.turns and not model.confirmed
        assert service.get_message(
            conversation.conversation_id, tenant_id="local-default",
            message_id=execution.initial.assistant_message.message_id,
        ).assistant_message.status == "FAILED"
    assert model.save_error is save_error


def test_supervisor_session_survives_api_switch_and_service_rebuild_with_full_missing_history(build_service):
    class SessionModel(_DirectSupervisorModel):
        def run_agent(self, prompt, *, session_context, **kwargs):
            self.context = session_context
            self.binding = dict(session_context.states.get("codex-A", {
                "threadId": "local-native-thread-A", "syncedThroughSequence": -1,
            }))
            self.loaded = session_context.load_history(self.binding["syncedThroughSequence"])
            session_context.save_state("codex-A", self.binding)
            return super().run_agent(prompt, **kwargs)

        def confirm_turn(self, thread_id, message_id, content):
            committed = repository.get_message_pair(thread_id, "local-default", message_id=message_id)
            assert committed.assistant_message.status == "COMPLETED"
            self.binding["syncedThroughSequence"] = self.context.message_sequence_no
            self.context.save_state("codex-A", self.binding)
            super().confirm_turn(thread_id, message_id, content)

    first_model = SessionModel()
    service, conversation, retrieval, repository, *_ = build_service(supervisor_model=first_model)
    first = _send(service, conversation, content="先用本地 Codex")
    binding = service.get_conversation(conversation.conversation_id, tenant_id="local-default").detail
    assert binding == {"runtimeSessions": {"codex-A": {
        "threadId": "local-native-thread-A", "syncedThroughSequence": first.assistant_message.sequence_no,
    }}}
    assert first_model.loaded == []

    class APIModel(_DirectSupervisorModel):
        def run_agent(self, prompt, **kwargs):
            if kwargs["user_message"] == "失败的 API 请求":
                raise RuntimeError("API unavailable")
            return super().run_agent(prompt, **kwargs)

    api_model = APIModel()
    service._runtime._model = api_model
    api_turns = [_send(service, conversation, key=f"api-{index}", content=f"API 对话 {index}")
                 for index in range(7)]
    with pytest.raises(AgentModelInvocationError):
        _send(service, conversation, key="api-failed", content="失败的 API 请求")
    assert service.get_conversation(conversation.conversation_id, tenant_id="local-default").detail == binding
    assert len(api_model.turns[-1][3]) == service._config.history_message_limit == 12
    committed = service.list_all_messages(conversation.conversation_id, tenant_id="local-default")
    failed = committed[-1]
    service.close()

    resumed_model = SessionModel()
    runtime = SupervisorRuntime(
        AgentRuntime(_QueryModel(), _EvaluatorModel(), _ChatModel(), retrieval.search, retrieval_channel="lexical"),
        resumed_model, WebSearcher(), max_web_results=5, web_timeout_seconds=12,
        execution_timeout_seconds=60, max_tool_result_bytes=16_384, max_prompt_chars=200_000,
    )
    rebuilt = AgentChatService(repository, service._library, runtime, config=service._config)
    try:
        resumed = _send(rebuilt, conversation, key="codex-resumed", content="切回原来的 Codex")
        expected = [item for item in committed
                    if item.status == "COMPLETED" and item.sequence_no > first.assistant_message.sequence_no]
        assert resumed_model.loaded == [{
            "role": item.role, "content": item.content or "", "messageId": item.message_id,
            "sequenceNo": item.sequence_no, "status": "COMPLETED",
        } for item in expected]
        assert len(resumed_model.loaded) == 15 > service._config.history_message_limit
        assert resumed_model.loaded[0]["messageId"] == api_turns[0].user_message.message_id
        assert failed.message_id not in {item["messageId"] for item in resumed_model.loaded}
        assert resumed_model.binding == {
            "threadId": "local-native-thread-A", "syncedThroughSequence": resumed.assistant_message.sequence_no,
        }
        assert rebuilt.get_conversation(conversation.conversation_id, tenant_id="local-default").detail == {
            "runtimeSessions": {"codex-A": resumed_model.binding},
        }
        assert all("runtimeSessions" not in prompt for prompt in resumed_model.prompts)
    finally:
        rebuilt.close()


def test_cancelled_session_callback_cannot_overwrite_a_later_completed_turn(build_service, monkeypatch):
    from threading import Thread

    saving, release_save, release_model = Event(), Event(), Event()
    late_errors = []

    class SessionModel(_DirectSupervisorModel):
        def run_agent(self, prompt, *, session_context, **kwargs):
            self.context = session_context
            if kwargs["user_message"] == "cancel this turn":
                def late_save():
                    try:
                        session_context.save_state("codex-A", {"threadId": "stale-thread"})
                    except Exception as exc:
                        late_errors.append(exc)

                self.late_worker = Thread(target=late_save)
                self.late_worker.start()
                assert release_model.wait(5), "test did not release cancelled model"
                kwargs["check_control"]()
            session_context.save_state("codex-A", {"threadId": "current-thread"})
            return super().run_agent(prompt, **kwargs)

        def confirm_turn(self, thread_id, message_id, content):
            self.context.save_state("codex-A", {
                "threadId": "current-thread", "syncedThroughSequence": self.context.message_sequence_no,
            })
            return super().confirm_turn(thread_id, message_id, content)

    model = SessionModel()
    service, conversation, _, repository, *_ = build_service(supervisor_model=model)
    save = repository.save_runtime_session

    def delayed_save(*args, **kwargs):
        if kwargs["state"].get("threadId") == "stale-thread":
            saving.set()
            assert release_save.wait(5), "test did not release stale session save"
        return save(*args, **kwargs)

    monkeypatch.setattr(repository, "save_runtime_session", delayed_save)
    cancelled = service.start_message(
        conversation.conversation_id, tenant_id="local-default",
        content="cancel this turn", idempotency_key="cancelled-session",
    )
    try:
        assert saving.wait(5), "session callback did not reach repository"
        service.cancel_message(
            conversation.conversation_id, tenant_id="local-default",
            message_id=cancelled.initial.assistant_message.message_id,
        )
        release_model.set()
        with pytest.raises(AgentModelInvocationError, match="cancelled"):
            cancelled.result()
        with pytest.raises(AgentConflictError, match="latest"):
            save(
                conversation.conversation_id, "local-default",
                message_id=cancelled.initial.assistant_message.message_id,
                key="codex-A", state={"threadId": "failed-thread"},
            )
        later = _send(service, conversation, key="later-session")
        expected = {"runtimeSessions": {"codex-A": {
            "threadId": "current-thread", "syncedThroughSequence": later.assistant_message.sequence_no,
        }}}
        assert service.get_conversation(conversation.conversation_id, tenant_id="local-default").detail == expected
        release_save.set()
        model.late_worker.join(5)
        assert not model.late_worker.is_alive()
        assert len(late_errors) == 1 and isinstance(late_errors[0], AgentConflictError)
        assert service.get_conversation(conversation.conversation_id, tenant_id="local-default").detail == expected
    finally:
        release_model.set()
        release_save.set()
        if hasattr(model, "late_worker"):
            model.late_worker.join(5)


def test_next_supervisor_turn_waits_for_previous_harness_confirmation(build_service, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    model = _DirectSupervisorModel()
    service, conversation, *_ = build_service(supervisor_model=model)
    confirming, release, next_started = Event(), Event(), Event()
    confirm = model.confirm_turn

    def confirm_first(thread_id, message_id, content):
        if not model.confirmed:
            confirming.set()
            assert release.wait(5), "test did not release confirmation"
        confirm(thread_id, message_id, content)

    def send_next():
        next_started.set()
        return _send(service, conversation, key="second", content="继续")

    monkeypatch.setattr(model, "confirm_turn", confirm_first)
    first = service.start_message(
        conversation.conversation_id, tenant_id="local-default",
        content="你好", idempotency_key="first",
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            assert confirming.wait(5), "confirmation did not start"
            second = executor.submit(send_next)
            assert next_started.wait(5), "next request did not start"
            with pytest.raises(TimeoutError):
                second.result(timeout=0.05)
            assert len(model.turns) == 1
        finally:
            release.set()
        assert first.result().assistant_message.status == "COMPLETED"
        assert second.result(timeout=5).assistant_message.status == "COMPLETED"
    assert len(model.confirmed) == 2


@pytest.mark.parametrize("failure", ["model", "commit", "cancel"])
def test_supervisor_discards_uncommitted_or_cancelled_turn(
    build_service, monkeypatch, failure,
):
    model = _DirectSupervisorModel()
    service, conversation, _, repository, *_ = build_service(supervisor_model=model)
    entered, release = Event(), Event()
    run_agent = model.run_agent

    def run(*args, **kwargs):
        run_agent(*args, **kwargs)
        if failure == "model":
            raise AgentModelInvocationError("injected model failure")
        if failure == "cancel":
            entered.set()
            assert release.wait(5), "test did not release provider"
            kwargs["check_control"]()

    monkeypatch.setattr(model, "run_agent", run)
    if failure == "commit":
        def fail_commit(*_args, **_kwargs):
            raise RuntimeError("injected commit failure")
        monkeypatch.setattr(repository, "complete_assistant_message", fail_commit)
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default",
        content="你好", idempotency_key="failed-supervisor", stream=True,
    )
    if failure == "cancel":
        try:
            assert entered.wait(5), "provider did not start"
            execution.cancellation.set()
        finally:
            release.set()
    events = _collect(execution)
    with pytest.raises((AgentModelInvocationError, RuntimeError)):
        execution.result()

    message_id = execution.initial.assistant_message.message_id
    assert model.discarded == [(conversation.conversation_id, message_id)]
    assert model.confirmed == []
    assert events[-1]["type"] == "answer.failed"
    failed = service.get_message(
        conversation.conversation_id, tenant_id="local-default", message_id=message_id,
    ).assistant_message
    snapshots = [event for event in events if event["type"] == "answer.snapshot"]
    assert snapshots and failed.content == snapshots[-1]["assistantMessage"]["content"]
    assert failed.status == "FAILED" and events[-1]["assistantMessage"] == asdict(failed)
    history = repository.list_recent_completed_messages(
        conversation.conversation_id, "local-default", before_sequence_no=100, limit=10,
    )
    assert all(message.message_id != failed.message_id for message in history)
    saved = service.list_all_messages(conversation.conversation_id, tenant_id="local-default")[-1]
    assert saved == failed


@pytest.mark.parametrize("failure", ["confirm", "event", "log"])
def test_supervisor_post_commit_failure_keeps_completed_sql_answer(
    build_service, monkeypatch, failure,
):
    from server.service.agent_chat import AgentMessageExecution

    model = _DirectSupervisorModel()
    service, conversation, _, repository, *_ = build_service(supervisor_model=model)
    failed_writes = []
    fail_message = repository.fail_assistant_message

    def record_failure(*args, **kwargs):
        failed_writes.append(args)
        return fail_message(*args, **kwargs)

    monkeypatch.setattr(repository, "fail_assistant_message", record_failure)
    if failure == "confirm":
        def fail_confirm(*_args):
            raise RuntimeError("injected confirmation failure")
        monkeypatch.setattr(model, "confirm_turn", fail_confirm)
    elif failure == "event":
        publish = AgentMessageExecution.publish

        def fail_completed_event(self, event):
            if event["type"] == "answer.completed":
                raise RuntimeError("injected event failure")
            publish(self, event)

        monkeypatch.setattr(AgentMessageExecution, "publish", fail_completed_event)
    else:
        log_info = logging.getLogger("server.service.agent_chat").info

        def fail_log(message, *args, **kwargs):
            if message.startswith("agent_execution_completed "):
                raise RuntimeError("injected completion log failure")
            log_info(message, *args, **kwargs)
        monkeypatch.setattr("server.service.agent_chat._logger.info", fail_log)

    completed = _send(service, conversation, content="你好")

    assert completed.assistant_message.status == "COMPLETED"
    assert service.get_message(
        conversation.conversation_id, tenant_id="local-default",
        message_id=completed.assistant_message.message_id,
    ) == completed
    assert failed_writes == []
    assert model.discarded == []
    assert model.deleted == ([conversation.conversation_id] if failure == "confirm" else [])


def test_supervisor_thread_is_deleted_only_after_authorized_sql_delete(build_service):
    entered, release = Event(), Event()

    class BlockingModel(_DirectSupervisorModel):
        def run_agent(self, *args, **kwargs):
            super().run_agent(*args, **kwargs)
            entered.set()
            assert release.wait(5), "test did not release provider"
            kwargs["check_control"]()

    model = BlockingModel()
    service, conversation, *_ = build_service(supervisor_model=model)
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default",
        content="你好", idempotency_key="pending-delete",
    )
    try:
        assert entered.wait(5), "provider did not start"
        with pytest.raises(AgentMessageInProgressError):
            service.delete_conversation(conversation.conversation_id, tenant_id="local-default")
        assert model.deleted == []
    finally:
        release.set()
    execution.result()

    for kwargs in [
        {"tenant_id": "another-workspace"},
        {"tenant_id": "local-default", "owner_user_id": "other-user"},
    ]:
        with pytest.raises(AgentConversationNotFoundError):
            service.delete_conversation(conversation.conversation_id, **kwargs)
    assert model.deleted == []
    deleted = service.delete_conversation(conversation.conversation_id, tenant_id="local-default")
    assert deleted.conversation_id == conversation.conversation_id
    assert model.deleted == [conversation.conversation_id]


@pytest.fixture
def evidence_service(build_service, prepared_import):
    resources = build_service()
    service, _, retrieval, repository, query, *_ = resources
    videos = VideoRepository(repository._session_factory)
    report = videos.import_video_parse(prepared_import)
    with repository._session_factory.begin() as session:
        session.get(Series, report.series_id).status = "READY"
    retrieval.hits = [VideoChunkSearchHit(
        candidate=VideoChunkCandidate(video_chunk_id=chunk.video_chunk_id, fused_rank=index + 1, fused_score=1),
        selection_reason=SelectionReason(summary=chunk.window_summary, matched_evidence=[], limitations=[]),
        evidence=chunk,
    ) for index, chunk in enumerate(videos.hydrate_video_chunks(report.video_chunk_ids))]
    query.selected_ranges = tuple(
        (
            hit.evidence.video_chunk_id,
            hit.evidence.start_seconds,
            hit.evidence.end_seconds,
        )
        for hit in retrieval.hits
    )
    return resources


def test_first_question_titles_only_default_conversations_and_creation_replay_is_stable(build_service):
    service, custom, *_ = build_service()
    default = service.create_conversation(tenant_id="local-default", idempotency_key="default-title")
    question = "寻找爽世哭泣并说出春日影的片段"
    _send(service, default, content=question)
    updated = service.get_conversation(default.conversation_id, tenant_id="local-default")
    assert updated.title == question
    assert updated.last_message_preview == "目前没有足够的候选画面，建议补充人物或动作。"
    assert service.create_conversation(tenant_id="local-default", idempotency_key="default-title") == updated
    _send(service, custom, key="custom-message", content=question)
    assert service.get_conversation(custom.conversation_id, tenant_id="local-default").title == "测试对话"


def test_query_agent_receives_series_transcript_language_hint(build_service):
    service, conversation, _retrieval, repository, query, *_ = build_service()
    with repository._session_factory.begin() as session:
        session.scalar(select(Series)).transcript_language_hint = "ja"

    _send(service, conversation, content="她当时的原话是什么？")

    context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])["inputs"]
    assert context["series_context"][0]["transcriptLanguageHint"] == "ja"
    assert "Lexical 通道不会翻译 Query" in query.prompts[-1]


def test_read_only_message_recovery_by_user_assistant_or_request_key(build_service):
    service, conversation, retrieval, *_ = build_service()
    result = _send(service, conversation)
    for identifier in ({"message_id": result.user_message.message_id},
                       {"message_id": result.assistant_message.message_id},
                       {"idempotency_key": "message"}):
        assert service.get_message(conversation.conversation_id, tenant_id="local-default", **identifier) == result
    assert len(retrieval.calls) == 1
    with pytest.raises(ValueError, match="exactly one"):
        service.get_message(conversation.conversation_id, tenant_id="local-default")
    with pytest.raises(AgentMessageNotFoundError):
        service.get_message(conversation.conversation_id, tenant_id="local-default", idempotency_key="not-accepted")
    with pytest.raises(AgentConversationNotFoundError):
        service.get_message(conversation.conversation_id, tenant_id="other-workspace", idempotency_key="message")


def test_reference_uses_saved_same_version_evidence_with_ordered_history(evidence_service):
    service, conversation, _, repository, query, *_ = evidence_service
    selected = list(query.selected_ranges)
    chunk_id, start_seconds, end_seconds = selected[1]
    selected[1] = (chunk_id, start_seconds + 1, end_seconds - 1)
    query.selected_ranges = tuple(selected)
    source = _send(service, conversation)
    hits = source.assistant_message.search_result["hits"]
    reference = {"messageId": source.assistant_message.message_id,
                 "videoChunkId": hits[1]["candidate"]["videoChunkId"]}
    with repository._session_factory.begin() as session:
        current = session.get(VideoChunkInfo, reference["videoChunkId"])
        assert current is not None
        current.context_before_compact = "newer current-version context"
    followup = _send(service, conversation, key="reference", content="这段的前文呢？", reference=reference)
    assert followup.user_message.reference == reference
    assert followup.assistant_message.reference is None
    assert _send(service, conversation, key="reference", content="这段的前文呢？", reference=reference) == followup
    context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])["inputs"]
    assert context["reference_context"]["evidence"]["videoChunkId"] == reference["videoChunkId"]
    assert context["reference_context"]["evidence"]["contextBeforeCompact"] == hits[1]["evidence"]["contextBeforeCompact"]
    assert context["reference_context"]["selectedRange"] == {
        "startSeconds": start_seconds + 1,
        "endSeconds": end_seconds - 1,
    }
    assert context["reference_context"]["sourceStatus"] == "COMPLETED"
    assert [hit["candidate"]["videoChunkId"] for hit in context["history"][1]["searchResult"]["hits"]] == [
        hit["candidate"]["videoChunkId"] for hit in hits
    ]
    assert context["history"][1]["searchResult"]["videoRanges"] == [
        {
            "videoChunkId": block["videoChunkId"],
            "startSeconds": block["startSeconds"],
            "endSeconds": block["endSeconds"],
        }
        for block in source.assistant_message.render_blocks
        if block["type"] == "video"
    ]
    changed = {**reference, "videoChunkId": hits[0]["candidate"]["videoChunkId"]}
    with pytest.raises(AgentConflictError):
        _send(service, conversation, key="reference", content="这段的前文呢？", reference=changed)


@pytest.mark.parametrize("legacy_key", ["workspaceId", "workspace_id"])
def test_historical_message_evidence_scope_is_read_without_changing_json(evidence_service, legacy_key):
    service, conversation, _, repository, query, *_ = evidence_service
    source = _send(service, conversation)
    assistant_id = source.assistant_message.message_id
    with repository._session_factory.begin() as session:
        record = session.get(AgentMessage, assistant_id)
        stored = json.loads(json.dumps(record.search_result))
        for hit in stored["hits"]:
            evidence = hit["evidence"]
            evidence[legacy_key] = evidence.pop("tenantId")
            evidence["importantExtraData"] = {"workspaceId": "unrelated-metadata"}
        record.search_result = stored
    restored = service.get_message(conversation.conversation_id, tenant_id="local-default", message_id=assistant_id)
    assert all(hit["evidence"]["tenantId"] == "local-default" for hit in restored.assistant_message.search_result["hits"])
    assert restored.assistant_message.search_result["hits"][0]["evidence"]["importantExtraData"] == {"workspaceId": "unrelated-metadata"}
    reference = {"messageId": assistant_id, "videoChunkId": restored.assistant_message.search_result["hits"][0]["candidate"]["videoChunkId"]}
    _send(service, conversation, key="legacy-reference", reference=reference)
    context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])["inputs"]
    assert context["reference_context"]["evidence"]["videoChunkId"] == reference["videoChunkId"]
    with repository._session_factory() as session:
        assert session.get(AgentMessage, assistant_id).search_result == stored


@pytest.mark.parametrize("conflicting_new_key", [None, "tenantId", "tenant_id"])
def test_historical_evidence_scope_rejects_cross_tenant_or_conflicting_aliases(evidence_service, conflicting_new_key):
    service, conversation, _, repository, *_ = evidence_service
    source = _send(service, conversation)
    assistant_id = source.assistant_message.message_id
    with repository._session_factory.begin() as session:
        record = session.get(AgentMessage, assistant_id)
        stored = json.loads(json.dumps(record.search_result))
        evidence = stored["hits"][0]["evidence"]
        evidence["workspaceId"] = "other-tenant"
        evidence.pop("tenantId")
        if conflicting_new_key is not None:
            evidence[conflicting_new_key] = "local-default"
        record.search_result = stored
    if conflicting_new_key is not None:
        with pytest.raises(AgentReferenceInvalidError, match="资源范围冲突"):
            service.get_message(conversation.conversation_id, tenant_id="local-default", message_id=assistant_id)
    else:
        reference = {"messageId": assistant_id, "videoChunkId": stored["hits"][0]["candidate"]["videoChunkId"]}
        with pytest.raises(AgentReferenceInvalidError, match="不属于本次检索范围"):
            _send(service, conversation, key="cross-tenant-legacy", reference=reference)


@pytest.mark.parametrize("blocks", [
    None,
    [{"type": "video", "videoChunkId": "chunk-1"}],
    [{
        "type": "video", "videoChunkId": "chunk-1",
        "startSeconds": 9, "endSeconds": 19,
    }],
    [{
        "type": "video", "videoChunkId": "chunk-1",
        "startSeconds": 18, "endSeconds": 17,
    }],
    [{
        "type": "video", "videoChunkId": "chunk-1",
        "startSeconds": 11, "endSeconds": 21,
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
            evidence_start_seconds=10,
            evidence_end_seconds=20,
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
    assert "evidence_start_seconds=10 evidence_end_seconds=20" in warning
    assert "action=reject_reference" in warning


def test_reference_range_preserves_valid_stored_range_without_warning(caplog):
    caplog.set_level(logging.WARNING, logger="server.service.agent_chat")

    selected_range = _validated_stored_video_range(
        {"renderBlocks": [{
            "type": "video",
            "videoChunkId": "chunk-1",
            "startSeconds": 11,
            "endSeconds": 19,
        }]},
        "chunk-1",
        evidence_start_seconds=10,
        evidence_end_seconds=20,
        conversation_id="conversation",
        message_id="assistant",
    )

    assert selected_range == {"startSeconds": 11, "endSeconds": 19}
    assert not [
        record for record in caplog.records
        if record.getMessage().startswith("event=agent_invalid_stored_video_range ")
    ]


@pytest.mark.parametrize(("sources", "neighbor_start", "accepted"), [
    (["left", "right"], 20, True),
    (None, 20, False),
    (["left", "missing"], 20, False),
    (["left", "right"], 21, False),
])
def test_reference_and_history_validate_saved_adjacent_sources(sources, neighbor_start, accepted):
    common = {"tenantId": "tenant", "seriesId": "series", "videoId": "video", "durationSeconds": 30}
    evidence = [
        {**common, "videoChunkId": "left", "sequenceNo": 0, "startSeconds": 10, "endSeconds": 20},
        {**common, "videoChunkId": "right", "sequenceNo": 1, "startSeconds": neighbor_start, "endSeconds": 30},
    ]
    block = {
        "type": "video", "videoChunkId": "left", "startSeconds": 15,
        "endSeconds": 25, "sourceVideoChunkIds": sources,
    }
    result = {
        "hits": [{"candidate": {"videoChunkId": item["videoChunkId"]}, "evidence": item} for item in evidence],
        "renderBlocks": [block],
    }
    if accepted:
        assert _validated_stored_video_range(
            result, "left", evidence_start_seconds=10, evidence_end_seconds=20,
        ) == {"startSeconds": 15, "endSeconds": 25}
    else:
        with pytest.raises(AgentReferenceInvalidError):
            _validated_stored_video_range(
                result, "left", evidence_start_seconds=10, evidence_end_seconds=20,
            )
    assert _project_stored_search_result(result)["videoRanges"] == (
        [{key: value for key, value in block.items() if key != "type"}] if accepted else []
    )


@pytest.mark.parametrize(("render_blocks", "expected"), [
    (None, []),
    ([{
        "type": "video",
        "videoChunkId": "chunk-1",
        "startSeconds": 9,
        "endSeconds": 19,
    }], []),
    ([{
        "type": "video",
        "videoChunkId": "unknown",
        "startSeconds": 11,
        "endSeconds": 19,
    }], []),
    ([{
        "type": "video",
        "videoChunkId": "chunk-1",
        "startSeconds": 11,
        "endSeconds": 19,
    }], [{"videoChunkId": "chunk-1", "startSeconds": 11, "endSeconds": 19}]),
])
def test_history_video_ranges_preserve_only_valid_stored_blocks(render_blocks, expected):
    result = {
        "query": "historical single query",
        "hits": [{
            "candidate": {"videoChunkId": "chunk-1", "fusedRank": 1},
            "selectionReason": {},
            "evidence": {"startSeconds": 10, "endSeconds": 20},
        }],
    }
    if render_blocks is not None:
        result["renderBlocks"] = render_blocks

    projected = _project_stored_search_result(result)
    assert "query" not in projected
    assert projected["videoRanges"] == expected


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
        target = service.create_conversation(tenant_id="local-default", title="独立聊天", idempotency_key="other")
    else:
        library = LibraryService(LibraryRepository(repository._session_factory))
        series = library.create_series(tenant_id="local-default", name="Other Series")
        with repository._session_factory.begin() as session:
            session.get(Series, series.series_id).status = "READY"
        scope = [series.series_id]
    before = service.list_all_messages(target.conversation_id, tenant_id="local-default")
    query_count = len(query.prompts)
    with pytest.raises(AgentReferenceInvalidError):
        _send(service, target, key="bad-reference", reference=reference, series_ids=scope)
    assert service.list_all_messages(target.conversation_id, tenant_id="local-default") == before
    assert len(query.prompts) == query_count


def test_agent_pages_count_filter_and_bind_cursor_to_query_and_scope(build_service):
    service, conversation, retrieval, *_ = build_service()
    first = _send(service, conversation)
    _send(service, conversation, key="second")
    initial = service.page_messages(conversation.conversation_id, tenant_id="local-default", limit=2)
    assert initial.total == 4 and initial.has_more and len(initial.items) == 2
    next_page = service.page_messages(conversation.conversation_id, tenant_id="local-default", limit=2,
                                      cursor=initial.next_cursor)
    assert [item.sequence_no for item in next_page.items] == [2, 3]
    assert next_page.total == 4 and not next_page.has_more and next_page.next_cursor is None
    with pytest.raises(ValueError):
        service.page_messages(conversation.conversation_id, tenant_id="local-default", limit=2,
                              cursor=initial.next_cursor, after_sequence_no=0)
    service.create_conversation(tenant_id="local-default", title="没搜索的对话", idempotency_key="unused")
    filtered = service.page_conversations(tenant_id="local-default", series_id=first.user_message.series_ids[0], query="测试")
    assert filtered.total == 1 and filtered.items[0].conversation_id == conversation.conversation_id
    assert service.page_conversations(tenant_id="local-default", query="不存在").total == 0
    assert len(retrieval.calls) == 2


def test_conversation_creation_is_idempotent_within_workspace(build_service):
    service, conversation, *_ = build_service()
    repeated = service.create_conversation(
        tenant_id="local-default", title="测试对话",
        idempotency_key="conversation",
    )
    assert repeated.conversation_id == conversation.conversation_id
    assert repeated.tenant_id == "local-default"
    assert [item.conversation_id for item in service.list_conversations(tenant_id="local-default")] == [conversation.conversation_id]
    with pytest.raises(AgentConflictError, match="different fields"):
        service.create_conversation(
            tenant_id="local-default", title="另一条对话",
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
            select(func.count()).select_from(AgentMessage)
        ) == 2
        assistant_detail = session.scalar(
            select(AgentMessage.detail).where(
                AgentMessage.role == "ASSISTANT"
            )
        )
        assert assistant_detail["timing"]["totalDurationMs"] is not None
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
            select(func.count()).select_from(AgentMessage)
        ) == 0


@pytest.mark.skipif(
    not os.getenv("ANIVAG_TEST_DATABASE_URL"),
    reason="ANIVAG_TEST_DATABASE_URL is not configured",
)
def test_mysql_agent_timing_roundtrips_through_message_detail() -> None:
    engine = create_database_engine(
        os.environ["ANIVAG_TEST_DATABASE_URL"],
        environment=DatabaseEnvironment.TEST,
    )
    conversation_id, user_id, assistant_id = (str(uuid4()) for _ in range(3))
    tenant_id = f"mysql-agent-timing-{uuid4()}"
    created = False
    try:
        create_schema(engine)
        assert "agent_message_timing" not in inspect(engine).get_table_names()
        factory = create_session_factory(engine)
        repository = AgentRepository(factory)
        repository.create_conversation(
            conversation_id=conversation_id, tenant_id=tenant_id,
            title="Timing roundtrip", idempotency_key="conversation",
        )
        created = True
        started_at = 1_700_000_000_000
        repository.start_message(
            conversation_id=conversation_id, tenant_id=tenant_id,
            content="Test timing", model_name="chat-test", requested_top_k=1,
            requested_run_options={}, idempotency_key="message",
            series_ids=[], requested_series_ids=[], user_message_id=user_id,
            assistant_message_id=assistant_id, now=started_at,
        )
        with factory() as session:
            assistant = session.get(AgentMessage, assistant_id)
            assert assistant.detail == {
                "timing": {"startedAt": started_at, "totalDurationMs": None, "stages": []}
            }
            initial_version = assistant.version
            initial_updated_at = assistant.update_time

        partial = AgentMessageTimingState(
            started_at=started_at, total_duration_ms=None,
            stages=(AgentStageTimingState(AnswerPhase.QUEUED, 4),),
        )
        repository.checkpoint_assistant_message(
            assistant_id, now=started_at + 4, timing=partial,
        )
        with factory() as session:
            assistant = session.get(AgentMessage, assistant_id)
            assert assistant.version == initial_version
            assert assistant.update_time == initial_updated_at
            assert assistant.detail["timing"]["stages"] == [
                {"phase": "QUEUED", "durationMs": 4}
            ]
        repository.checkpoint_assistant_message(
            assistant_id, now=started_at + 4, phase=AnswerPhase.RESPONDING,
            timing=partial,
        )
        with factory() as session:
            assert session.get(AgentMessage, assistant_id).detail["timing"]["stages"] == [
                {"phase": "QUEUED", "durationMs": 4}
            ]

        finished = AgentMessageTimingState(
            started_at=started_at, total_duration_ms=12,
            stages=(
                AgentStageTimingState(AnswerPhase.QUEUED, 4),
                AgentStageTimingState(AnswerPhase.RESPONDING, 8),
            ),
        )
        result = repository.complete_assistant_message(
            assistant_id, content="Done",
            search_result=None, now=started_at + 12, timing=finished,
        )
        assert result.timing == finished
        with factory() as session:
            assert session.get(AgentMessage, assistant_id).detail == {"timing": {
                "startedAt": started_at, "totalDurationMs": 12,
                "stages": [
                    {"phase": "QUEUED", "durationMs": 4},
                    {"phase": "RESPONDING", "durationMs": 8},
                ],
            }}
    finally:
        if created:
            with factory.begin() as session:
                session.execute(delete(AgentConversation).where(
                    AgentConversation.id == conversation_id
                ))
        engine.dispose()


def test_service_delete_is_workspace_scoped_and_rejects_processing_answer(build_service):
    release = Event()
    service, conversation, _, _, _, _, _ = build_service(
        chat=_ChatModel(release=release)
    )
    execution = service.start_message(
        conversation.conversation_id,
        tenant_id="local-default",
        content="找到杯子",
        idempotency_key="processing-delete",
        stream=True,
    )
    try:
        with pytest.raises(AgentConversationNotFoundError):
            service.delete_conversation(
                conversation.conversation_id,
                tenant_id="another-workspace",
            )
        with pytest.raises(AgentMessageInProgressError):
            service.delete_conversation(
                conversation.conversation_id,
                tenant_id="local-default",
            )
        assert service.get_conversation(
            conversation.conversation_id,
            tenant_id="local-default",
        ).message_count == 2
    finally:
        release.set()
    execution.result()

    deleted = service.delete_conversation(
        conversation.conversation_id,
        tenant_id="local-default",
    )
    assert deleted.conversation_id == conversation.conversation_id
    assert deleted.message_count == 2
    with pytest.raises(AgentConversationNotFoundError):
        service.get_conversation(
            conversation.conversation_id,
            tenant_id="local-default",
        )


def test_conversation_reads_and_execution_do_not_cross_workspaces(build_service):
    service, conversation, retrieval, _, query, *_ = build_service()
    assert service.list_conversations(tenant_id="another-workspace") == []
    for read in [service.get_conversation, service.list_messages, service.list_all_messages]:
        with pytest.raises(AgentConversationNotFoundError):
            read(conversation.conversation_id, tenant_id="another-workspace")
    with pytest.raises(AgentConversationNotFoundError):
        service.send_message(
            conversation.conversation_id, tenant_id="another-workspace",
            content="寻找摔杯子的片段", idempotency_key="message",
        )
    assert not retrieval.calls and not query.prompts
    assert service.list_all_messages(conversation.conversation_id, tenant_id="local-default") == []


@pytest.mark.parametrize("second_query", ["补充摔杯子", None])
def test_reviewed_chat_is_persisted_and_idempotent(build_service, second_query):
    service, conversation, retrieval, repository, query, evaluator, chat = build_service(
        query=_QueryModel(queries=("第一次改写", second_query)),
        evaluator=_EvaluatorModel(approve_after=2),
    )
    first = _send(service, conversation)
    repeated = _send(service, conversation)
    messages = service.list_messages(conversation.conversation_id, tenant_id="local-default")
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
    with repository._session_factory() as session:
        stored = session.get(AgentMessage, messages[1].message_id)
        assert stored.detail == {"renderBlocks": list(messages[1].render_blocks), "timing": {
            "startedAt": timing.started_at,
            "totalDurationMs": timing.total_duration_ms,
            "stages": [
                {"phase": stage.phase.value, "durationMs": stage.duration_ms}
                for stage in timing.stages
            ],
        }}
    assert [call[0] for call in retrieval.calls] == ["第一次改写"] + ([second_query] if second_query else [])
    assert evaluator.calls == 2 and len(chat.prompts) == 1
    context = json.loads(query.prompts[1].rsplit("\n\n", 1)[1])
    assert context["original_plan"] == "第 1 轮检索并核验动作。"
    assert context["turns"][0]["draft"]["message"] == "第 1 轮待审核草稿。"
    assert context["turns"][0]["review"]["advice"] == "补充摔杯子的动作，重新核验。"
    with pytest.raises(AgentConflictError, match="different request fields"):
        service.send_message(conversation.conversation_id, tenant_id="local-default", content="寻找摔杯子的片段", top_k=7, idempotency_key="message")
    with pytest.raises(AgentConflictError, match="different request fields"):
        service.send_message(
            conversation.conversation_id,
            tenant_id="local-default",
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
        tenant_id="local-default",
        content="寻找摔杯子的片段",
        idempotency_key="custom-run-options",
        max_model_calls_per_turn=9,
        max_tool_calls_per_turn=33,
    )

    assert result.assistant_message.status == "COMPLETED"
    assert observed == [(9, 33, None)]
    with repository._session_factory() as session:
        user = session.get(AgentMessage, result.user_message.message_id)
        assert user is not None
        assert user.requested_run_options == {
            "max_model_calls_per_turn": 9,
            "max_tool_calls_per_turn": 33,
        }
        assert user.detail["request"]["idempotencyKey"] == "custom-run-options"
    service.send_message(
        conversation.conversation_id,
        tenant_id="local-default",
        content="再找一个片段",
        idempotency_key="auto-loop-options",
        max_loop_turns=0,
    )
    service.send_message(
        conversation.conversation_id,
        tenant_id="local-default",
        content="换个片段",
        idempotency_key="custom-loop-options",
        max_loop_turns=100,
    )
    assert observed[-2:] == [(6, 24, 0), (6, 24, 100)]
    with repository._session_factory() as session:
        users = session.scalars(select(AgentMessage).where(
            AgentMessage.conversation_id == conversation.conversation_id,
            AgentMessage.role == "USER",
        )).all()
        snapshots = {
            user.detail["request"]["idempotencyKey"]: user.requested_run_options
            for user in users
        }
    assert snapshots["auto-loop-options"]["max_loop_turns"] == 0
    assert snapshots["custom-loop-options"]["max_loop_turns"] == 100
    with pytest.raises(AgentConflictError, match="different request fields"):
        service.send_message(
            conversation.conversation_id,
            tenant_id="local-default",
            content="换个片段",
            idempotency_key="custom-loop-options",
            max_loop_turns=0,
        )
    with pytest.raises(ValueError, match="between 0 and 100"):
        service.send_message(
            conversation.conversation_id,
            tenant_id="local-default",
            content="轮次过高",
            idempotency_key="over-loop-limit",
            max_loop_turns=101,
        )
    with pytest.raises(ValueError, match="between 1 and 12"):
        service.send_message(
            conversation.conversation_id,
            tenant_id="local-default",
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
    messages = service.list_all_messages(conversation.conversation_id, tenant_id="local-default")
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
        conversation.conversation_id, tenant_id="local-default",
    )
    assert messages[-1].status == "FAILED"
    assert messages[-1].last_error.code == "AGENT_CONTEXT_BUDGET_EXCEEDED"
    assert "精简系列背景" in messages[-1].last_error.message
    assert query.prompts == [] and evaluator.calls == 0 and chat.prompts == []


def test_execution_timeout_persists_specific_failure_and_publishes_terminal_event(build_service, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("agent.chat.video_search.runtime.monotonic", lambda: clock[0])
    monkeypatch.setattr("agent.chat.common.state.monotonic", lambda: clock[0])

    class SlowEvaluator(_EvaluatorModel):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            clock[0] += 600
            return result

    service, conversation, _, _, query, evaluator, chat = build_service(
        evaluator=SlowEvaluator(),
        runtime_kwargs={
            "total_timeout_enabled": True, "execution_timeout_seconds": 600,
            "evaluator_stage_timeout_seconds": 600,
        },
    )
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default",
        content="找杯子", idempotency_key="timeout-stream", stream=True,
    )
    events = _collect(execution)

    with pytest.raises(AgentExecutionTimeoutError, match="deadline exceeded"):
        execution.result()

    failed = service.list_all_messages(
        conversation.conversation_id, tenant_id="local-default",
    )[-1]
    assert clock[0] == 700.0
    assert len(query.prompts) == evaluator.calls == 1 and chat.prompts == []
    assert failed.message_id == execution.initial.assistant_message.message_id
    assert failed.status == "FAILED" and failed.phase is None
    assert failed.content is None and failed.search_result is None
    assert failed.last_error.code == "AGENT_EXECUTION_TIMEOUT"
    assert failed.last_error.message == "本次处理超时，已停止生成，请缩小问题后重试。"
    assert events[-1]["type"] == "answer.failed"
    assert events[-1]["error"] == asdict(failed.last_error)
    assert not {"answer.completed", "answer.delta", "answer.block", "answer.evidence"} & {
        event["type"] for event in events
    }


def test_codex_connection_error_fails_before_deadline_and_publishes_terminal_event(build_service):
    from gateway.codex.agent import CodexAgentModel
    from gateway.codex.supervisor import CodexSupervisorModel

    started, closed = Event(), Event()
    connection_error = RuntimeError("proxy CONNECT failed: HTTP 403 Forbidden")

    class FailedConnectionSession:
        tool_call_count = 0
        successful_tool_call_count = 0
        native_thread_id = "connection-error-thread"
        native_last_turn_id = None

        def open(self):
            pass

        def run_turn(self, _prompt, *, output_schema=None):
            started.set()
            assert closed.wait(5), "Codex connection failure did not close its transport"
            raise RuntimeError("transport closed")

        def raise_if_failed(self):
            if started.is_set():
                raise connection_error

        def close(self):
            closed.set()

    session = FailedConnectionSession()
    model = CodexAgentModel(
        model="gpt-6-astra", request_timeout_seconds=600, max_output_tokens=256,
        max_input_chars=200_000, max_concurrency=1,
        session_factory=lambda **_kwargs: session,
    )
    model.supports_vision_input = True
    service, conversation, _, repository, query, evaluator, chat = build_service(
        supervisor_model=CodexSupervisorModel(agent_model=model),
    )
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default", content="你好",
        idempotency_key="codex-connection-error", stream=True,
    )
    try:
        assert started.wait(2), "Codex invocation did not start"
        with pytest.raises(AgentModelInvocationError) as caught:
            execution.future.result(timeout=3)
        events = _collect(execution)
    finally:
        session.close()

    root_error = caught.value
    while root_error.__cause__ is not None:
        root_error = root_error.__cause__
    assert root_error is connection_error
    failed = service.get_message(
        conversation.conversation_id, tenant_id="local-default",
        message_id=execution.initial.assistant_message.message_id,
    ).assistant_message
    assert closed.is_set()
    assert failed.status == "FAILED" and failed.phase is None
    assert failed.last_error.code == "AGENT_MODEL_ERROR"
    assert failed.content is None and failed.search_result is None
    with repository._session_factory() as sql_session:
        record = sql_session.get(AgentMessage, failed.message_id)
        assert record.status == "FAILED" and record.phase is None
        assert record.last_error_code == "AGENT_MODEL_ERROR"
    assert events[-1]["type"] == "answer.failed"
    assert events[-1]["error"] == asdict(failed.last_error)
    assert "answer.completed" not in {event["type"] for event in events}
    assert query.prompts == [] and evaluator.calls == 0 and chat.prompts == []


def test_manual_cancel_is_scoped_and_stops_before_the_next_model(build_service):
    entered, release = Event(), Event()

    class BlockingQuery(_QueryModel):
        def run_structured(self, prompt, **kwargs):
            result = super().run_structured(prompt, **kwargs)
            entered.set()
            assert release.wait(5), "test did not release Query"
            return result

    service, conversation, _, _, query, evaluator, chat = build_service(query=BlockingQuery())
    other = service.create_conversation(tenant_id="local-default", idempotency_key="other")
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default", content="找杯子",
        idempotency_key="cancel-query", stream=True,
    )
    message_id = execution.initial.assistant_message.message_id
    try:
        assert entered.wait(5), "Query did not start"
        for kwargs in (
            {"tenant_id": "other-workspace"},
            {"tenant_id": "local-default", "owner_user_id": "other-user"},
        ):
            with pytest.raises(AgentConversationNotFoundError):
                service.cancel_message(conversation.conversation_id, message_id=message_id, **kwargs)
        with pytest.raises(AgentMessageNotFoundError):
            service.cancel_message(other.conversation_id, tenant_id="local-default", message_id=message_id)
        with pytest.raises(ValueError, match="only assistant"):
            service.cancel_message(
                conversation.conversation_id, tenant_id="local-default",
                message_id=execution.initial.user_message.message_id,
            )
        assert not execution.cancellation.is_set()

        stopping = service.cancel_message(
            conversation.conversation_id, tenant_id="local-default", message_id=message_id,
        )
        assert stopping.assistant_message.status == "PROCESSING"
        assert execution.cancellation.is_set() and not execution.future.done()
        assert service.cancel_message(
            conversation.conversation_id, tenant_id="local-default", message_id=message_id,
        ) == stopping
    finally:
        release.set()

    events = _collect(execution)
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        execution.result()
    failed = service.get_message(
        conversation.conversation_id, tenant_id="local-default", message_id=message_id,
    ).assistant_message
    assert len(query.prompts) == 1 and evaluator.calls == 0 and chat.prompts == []
    assert failed.status == "FAILED" and failed.content is None and failed.search_result is None
    assert failed.last_error.code == "AGENT_CANCELLED"
    assert failed.last_error.message == "已停止生成回答。"
    assert events[-1]["type"] == "answer.failed"
    assert events[-1]["error"] == asdict(failed.last_error)
    assert not {"answer.completed", "answer.delta", "answer.block", "answer.evidence"} & {
        event["type"] for event in events
    }


@pytest.mark.parametrize("failed", [False, True])
def test_manual_cancel_is_idempotent_for_terminal_replies(build_service, monkeypatch, failed):
    service, conversation, _, repository, *_ = build_service(chat=_ChatModel(fail=failed))
    if failed:
        with pytest.raises(AgentModelInvocationError):
            _send(service, conversation)
    else:
        _send(service, conversation)
    pair = service.get_message(conversation.conversation_id, tenant_id="local-default", idempotency_key="message")

    def forbidden_write(*_args, **_kwargs):
        raise AssertionError("terminal cancellation attempted a write")

    monkeypatch.setattr(repository, "fail_assistant_message", forbidden_write)
    for _ in range(2):
        assert service.cancel_message(
            conversation.conversation_id, tenant_id="local-default",
            message_id=pair.assistant_message.message_id,
        ) == pair


def test_manual_cancel_finishes_an_orphaned_processing_reply(build_service):
    service, conversation, _, repository, query, evaluator, chat = build_service()
    started = repository.start_message(
        conversation_id=conversation.conversation_id, tenant_id="local-default",
        content="找杯子", model_name=service.model_name, requested_top_k=3,
        requested_run_options={"max_model_calls_per_turn": 6, "max_tool_calls_per_turn": 24},
        series_ids=[], requested_series_ids=[], idempotency_key="orphaned-reply",
        user_message_id=str(uuid4()), assistant_message_id=str(uuid4()), now=service._clock(),
    )

    cancelled = service.cancel_message(
        conversation.conversation_id, tenant_id="local-default",
        message_id=started.assistant_message.message_id,
    )

    assert cancelled.assistant_message.status == "FAILED"
    assert cancelled.assistant_message.last_error.code == "AGENT_CANCELLED"
    assert cancelled.assistant_message.timing.total_duration_ms is not None
    assert query.prompts == chat.prompts == [] and evaluator.calls == 0
    assert service.cancel_message(
        conversation.conversation_id, tenant_id="local-default",
        message_id=started.assistant_message.message_id,
    ) == cancelled


@pytest.mark.parametrize("timeout", [False, True])
def test_manual_cancel_before_commit_prevents_completion_and_preserves_timeout(
    build_service, monkeypatch, timeout,
):
    entered, release = Event(), Event()
    service, conversation, *_ = build_service()
    kick = service._runtime.kick

    def pause_before_commit(*args, **kwargs):
        final = kick(*args, **kwargs)
        entered.set()
        assert release.wait(5), "test did not release final result"
        if timeout:
            raise AgentExecutionTimeoutError("Agent execution deadline exceeded")
        return final

    monkeypatch.setattr(service._runtime, "kick", pause_before_commit)
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default", content="找杯子",
        idempotency_key="cancel-before-commit", stream=True,
    )
    try:
        assert entered.wait(5), "runtime did not finish"
        service.cancel_message(
            conversation.conversation_id, tenant_id="local-default",
            message_id=execution.initial.assistant_message.message_id,
        )
    finally:
        release.set()
    events = _collect(execution)
    expected = AgentExecutionTimeoutError if timeout else AgentModelInvocationError
    with pytest.raises(expected):
        execution.result()
    failed = service.get_message(
        conversation.conversation_id, tenant_id="local-default",
        message_id=execution.initial.assistant_message.message_id,
    ).assistant_message
    assert failed.status == "FAILED" and failed.content is None
    assert failed.last_error.code == ("AGENT_EXECUTION_TIMEOUT" if timeout else "AGENT_CANCELLED")
    assert events[-1]["type"] == "answer.failed"
    assert not {"answer.completed", "answer.delta", "answer.block", "answer.evidence"} & {
        event["type"] for event in events
    }


def test_manual_cancel_after_commit_has_started_observes_completed_reply(build_service, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    entered, release, cancel_started = Event(), Event(), Event()
    service, conversation, _, repository, *_ = build_service()
    complete = repository.complete_assistant_message

    def blocked_commit(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release commit"
        return complete(*args, **kwargs)

    monkeypatch.setattr(repository, "complete_assistant_message", blocked_commit)
    execution = service.start_message(
        conversation.conversation_id, tenant_id="local-default", content="找杯子",
        idempotency_key="commit-before-cancel",
    )

    def cancel():
        cancel_started.set()
        return service.cancel_message(
            conversation.conversation_id, tenant_id="local-default",
            message_id=execution.initial.assistant_message.message_id,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            assert entered.wait(5), "commit did not start"
            pending = executor.submit(cancel)
            assert cancel_started.wait(5), "cancel request did not start"
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.05)
        finally:
            release.set()
        completed = execution.result()
        assert pending.result(timeout=5) == completed
    assert completed.assistant_message.status == "COMPLETED"
    assert not execution.cancellation.is_set()


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
            "seriesIds": first.assistant_message.search_result["seriesIds"],
            "resultCount": 0, "hits": [], "videoRanges": [],
            "partial": False, "warnings": [],
        },
    }
    with pytest.raises(AgentConversationNotFoundError):
        service.send_message(conversation.conversation_id, tenant_id="other-workspace", content="隔离", idempotency_key="other")


def test_all_scope_is_frozen_across_turns_and_replay_then_refreshes_for_next_kick(
    build_service, monkeypatch,
):
    service, conversation, retrieval, repository, query, _, _ = build_service(
        evaluator=_EvaluatorModel(approve_after=2),
    )
    library = LibraryService(LibraryRepository(repository._session_factory))
    original_id = library.list_series(tenant_id="local-default")[0].series_id
    run_structured = query.run_structured
    added_ids = []

    def query_with_new_series(*args, **kwargs):
        draft = run_structured(*args, **kwargs)
        if not added_ids:
            added = library.create_series(tenant_id="local-default", name="Added mid-loop")
            with repository._session_factory.begin() as session:
                session.get(Series, added.series_id).status = "READY"
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
        session.get(Series, original_id).status = "INDEXING"
    assert _send(service, conversation) == first
    assert len(retrieval.calls) == 2
    with repository._session_factory.begin() as session:
        session.get(Series, original_id).status = "READY"

    second = _send(service, conversation, key="next-all", series_ids=[])
    assert second.user_message.series_ids == tuple(sorted([original_id, added_ids[0]]))
    assert retrieval.calls[-1][1] == list(second.user_message.series_ids)
    context = json.loads(query.prompts[-1].rsplit("\n\n", 1)[1])
    assert [item["seriesIds"] for item in context["inputs"]["history"]] == [[original_id], [original_id]]


def test_next_message_can_choose_another_scope_without_reusing_old_evidence(build_service):
    service, conversation, retrieval, repository, query, _, _ = build_service()
    library = LibraryService(LibraryRepository(repository._session_factory))
    original_id = library.list_series(tenant_id="local-default")[0].series_id
    added = library.create_series(tenant_id="local-default", name="Second animation")
    with repository._session_factory.begin() as session:
        session.get(Series, added.series_id).status = "READY"

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
    execution = service.start_message(conversation.conversation_id, tenant_id="local-default", content="找到杯子", idempotency_key="stream", stream=True)
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
            conversation.conversation_id, tenant_id="local-default",
        )[-1]
        assert processing.status == "PROCESSING"
        assert processing.timing is not None
        assert processing.timing.total_duration_ms is None
        assert [stage.phase.value for stage in processing.timing.stages] == [
            "QUEUED", "RETRIEVING", "EVALUATING",
        ]
        with pytest.raises(AgentMessageInProgressError):
            service.start_message(conversation.conversation_id, tenant_id="local-default", content="找到杯子", idempotency_key="stream")
        with pytest.raises(AgentMessageInProgressError):
            service.start_message(conversation.conversation_id, tenant_id="local-default", content="另一个问题", idempotency_key="other")
        execution.detach()
        assert not execution.cancellation.is_set()
    finally:
        release.set()
    assert execution.result().assistant_message.status == "COMPLETED"


def test_completed_event_observes_committed_snapshot(build_service):
    service, conversation, retrieval, _, _, _, _ = build_service()
    execution = service.start_message(conversation.conversation_id, tenant_id="local-default", content="找杯子", idempotency_key="stream", stream=True)
    events = _collect(execution)
    assert events[-1]["type"] == "answer.completed"
    completed = execution.result().assistant_message
    persisted = service.list_all_messages(conversation.conversation_id, tenant_id="local-default")[-1]
    assert persisted == completed
    assert events[-1]["assistantMessage"]["status"] == "COMPLETED"
    assert [event["type"] for event in events].count("answer.evidence") == 1
    evidence = next(event for event in events if event["type"] == "answer.evidence")
    assert evidence["searchResult"] == completed.search_result
    assert "query" not in completed.search_result
    assert service.default_top_k == 3
    assert [call[2] for call in retrieval.calls] == [8]
    assert completed.search_result["retrievalTrace"] == [{
        "query": "第一次改写",
        "topK": 8,
        "strategyVersion": "query-adaptive-three-channel-rrf-v2",
        "requestedWeights": {"lexical": 0.1, "semantic": 0.5, "visual": 0.4},
        "effectiveWeights": {"lexical": 0.1, "semantic": 0.5, "visual": 0.4},
        "resultCount": 0,
        "videoChunkIds": [],
    }]
    blocks = [event["block"] for event in events if event["type"] == "answer.block"]
    assert blocks == list(completed.render_blocks)
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
    execution = service.start_message(conversation.conversation_id, tenant_id="local-default", content="找杯子", idempotency_key="stream", stream=True)
    events = _collect(execution)
    with pytest.raises((RuntimeError, AgentModelInvocationError)):
        execution.result()
    assert events[-1]["type"] == "answer.failed"
    assert "answer.completed" not in [event["type"] for event in events]
    failed = service.list_all_messages(conversation.conversation_id, tenant_id="local-default")[-1]
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
    execution = service.start_message(conversation.conversation_id, tenant_id="local-default",
                                      content="找画面", idempotency_key="unknown-terminal", stream=True)
    events = _collect(execution)
    with pytest.raises(AgentModelInvocationError):
        execution.result()
    assert events[-1]["type"] == "answer.interrupted"
    assert not {"userMessage", "assistantMessage"} & set(events[-1])
    assert not {"answer.completed", "answer.failed"} & {event["type"] for event in events}
    recovered = service.get_message(conversation.conversation_id, tenant_id="local-default",
                                    idempotency_key="unknown-terminal").assistant_message
    assert recovered.status == "PROCESSING" and recovered.search_result is None
    assert (recovered.content or "") == "".join(
        event["delta"] for event in events if event["type"] == "answer.delta"
    )


def test_slow_consumer_receives_one_atomic_tool_answer(build_service):
    service, conversation, *_ = build_service()
    execution = service.start_message(conversation.conversation_id, tenant_id="local-default",
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
    assert service.get_message(conversation.conversation_id, tenant_id="local-default",
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
    execution = service.start_message(conversation.conversation_id, tenant_id="local-default", content="找杯子", idempotency_key="shutdown", stream=True)
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
    assert service.list_all_messages(conversation.conversation_id, tenant_id="local-default")[-1].status == "FAILED"
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
        retrieval_channel="lexical",
    )
    history_limits = []
    original_history = repository.list_recent_completed_messages

    def record_history_limit(*args, **kwargs):
        history_limits.append(kwargs["limit"])
        return original_history(*args, **kwargs)

    repository.list_recent_completed_messages = record_history_limit
    before = service.start_message(
        conversation.conversation_id, tenant_id="local-default",
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
            tenant_id="local-default", idempotency_key="next-conversation",
        )
        after = service.start_message(
            next_conversation.conversation_id, tenant_id="local-default",
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
    series = library.create_series(tenant_id="workspace-1", name="Long Conversation")
    service = AgentChatService(
        repository,
        library,
        AgentRuntime(
            _QueryModel(),
            _EvaluatorModel(),
            _ChatModel(),
            _RetrievalStub().search,
            retrieval_channel="lexical",
        ),
    )
    conversation = service.create_conversation(
        tenant_id="workspace-1",
        title="History",
        idempotency_key="conversation",
    )
    with session_factory.begin() as db_session:
        db_session.add_all(
            AgentMessage(
                id=str(uuid4()),
                conversation_id=conversation.conversation_id,
                sequence_no=index,
                role="USER" if index % 2 == 0 else "ASSISTANT",
                status="COMPLETED",
                content=f"history {index}",
                series_ids=[series.series_id],
                requested_series_ids=[series.series_id] if index % 2 == 0 else None,
                model=None if index % 2 == 0 else "historical-model",
            )
            for index in range(500)
        )
        record = db_session.get(AgentConversation, conversation.conversation_id)
        assert record is not None
        record.next_message_sequence = 500

    now = 1_789_454_400_000
    started = repository.start_message(
        conversation_id=conversation.conversation_id,
        tenant_id="workspace-1",
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
        conversation.conversation_id, tenant_id="workspace-1", limit=500
    )
    snapshot = service.list_all_messages(
        conversation.conversation_id, tenant_id="workspace-1"
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
            search_result={"hits": []},
            now=1_789_454_399_000,
        )
    else:
        repository.fail_assistant_message(
            started.assistant_message.message_id, error="failed",
            now=1_789_454_399_000,
        )
    final = service.list_all_messages(
        conversation.conversation_id, tenant_id="workspace-1"
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
            conversation.conversation_id, tenant_id="another-workspace"
        )

def test_conversation_detail_roundtrips_without_changing_message_detail_or_owner_scope(build_service):
    service, conversation, _, repository, *_ = build_service()
    assert conversation.detail is None
    detail = {
        "environment": "local",
        "runtime": {"type": "codex", "threadId": "thread-1", "sessionId": "thread-1", "cwd": "/tmp/notes"},
        "custom": {"label": "课程笔记"},
    }
    with repository._session_factory.begin() as session:
        session.get(AgentConversation, conversation.conversation_id).detail = detail

    state = service.get_conversation(conversation.conversation_id, tenant_id="local-default")
    assert state.detail == detail
    assert service.list_conversations(tenant_id="local-default")[0].detail == detail
    assert service.page_conversations(tenant_id="local-default").items[0].detail == detail
    assert service.create_conversation(
        tenant_id="local-default", title="测试对话", idempotency_key="conversation",
    ).detail == detail
    with pytest.raises(AgentConversationNotFoundError):
        service.get_conversation(conversation.conversation_id, tenant_id="local-default", owner_user_id="other-user")
    with pytest.raises(AgentConversationNotFoundError):
        service.get_conversation(conversation.conversation_id, tenant_id="other-tenant")

    _send(service, conversation)
    assert service.get_conversation(conversation.conversation_id, tenant_id="local-default").detail == detail
    with repository._session_factory() as session:
        messages = session.scalars(select(AgentMessage)).all()
        assert messages and all("runtime" not in (message.detail or {}) for message in messages)
    assert service.delete_conversation(conversation.conversation_id, tenant_id="local-default").detail == detail


def test_runtime_session_updates_merge_conversation_detail_and_preserve_owner_scope(build_service):
    service, conversation, _, repository, *_ = build_service()
    first = _send(service, conversation)
    message_id = first.assistant_message.message_id
    with repository._session_factory.begin() as session:
        session.get(AgentConversation, conversation.conversation_id).detail = {
            "custom": {"label": "保留"}, "runtimeSessions": {"other": {"threadId": "other-thread"}},
        }
    state = {"threadId": "native-A", "syncedThroughSequence": first.assistant_message.sequence_no}
    repository.save_runtime_session(
        conversation.conversation_id, "local-default", message_id=message_id, key="codex-A", state=state,
    )
    state["threadId"] = "mutated"
    repository.save_runtime_session(
        conversation.conversation_id, "local-default", message_id=message_id,
        key="codex-B", state={"threadId": "native-B"},
    )
    expected = {"custom": {"label": "保留"}, "runtimeSessions": {
        "other": {"threadId": "other-thread"},
        "codex-A": {"threadId": "native-A", "syncedThroughSequence": first.assistant_message.sequence_no},
        "codex-B": {"threadId": "native-B"},
    }}
    assert service.get_conversation(conversation.conversation_id, tenant_id="local-default").detail == expected
    for scope in ({"tenant_id": "other"}, {"tenant_id": "local-default", "owner_user_id": "other"}):
        with pytest.raises(AgentConversationNotFoundError):
            repository.save_runtime_session(
                conversation.conversation_id, message_id=message_id, key="codex-A", state={}, **scope,
            )
        with pytest.raises(AgentConversationNotFoundError):
            repository.list_recent_completed_messages(
                conversation.conversation_id, before_sequence_no=100, after_sequence_no=-1, limit=None, **scope,
            )
    assert service.get_conversation(conversation.conversation_id, tenant_id="local-default").detail == expected
    _send(service, conversation, key="later-turn")
    with pytest.raises(AgentConflictError, match="latest"):
        repository.save_runtime_session(
            conversation.conversation_id, "local-default", message_id=message_id, key="codex-A", state={},
        )
    with pytest.raises(AgentMessageNotFoundError, match="assistant"):
        repository.save_runtime_session(
            conversation.conversation_id, "local-default", message_id=first.user_message.message_id,
            key="codex-A", state={},
        )
    assert service.get_conversation(conversation.conversation_id, tenant_id="local-default").detail == expected


def test_public_tenant_conversations_are_private_per_owner(build_service):
    service, _, _, repository, *_ = build_service()
    first = service.create_conversation(
        tenant_id="local-default", owner_user_id="guest-A",
        idempotency_key="shared-creation-key",
    )
    second = service.create_conversation(
        tenant_id="local-default", owner_user_id="guest-B",
        idempotency_key="shared-creation-key",
    )
    assert first.conversation_id != second.conversation_id
    assert service.create_conversation(
        tenant_id="local-default", owner_user_id="guest-A",
        idempotency_key="shared-creation-key",
    ).conversation_id == first.conversation_id
    answer = service.send_message(
        first.conversation_id, tenant_id="local-default", owner_user_id="guest-A",
        content="private question", idempotency_key="private-message",
    )
    assert [item.conversation_id for item in service.page_conversations(
        tenant_id="local-default", owner_user_id="guest-B",
    ).items] == [second.conversation_id]
    checks = (
        lambda: service.get_conversation(first.conversation_id, tenant_id="local-default", owner_user_id="guest-B"),
        lambda: service.page_messages(first.conversation_id, tenant_id="local-default", owner_user_id="guest-B"),
        lambda: service.list_messages(first.conversation_id, tenant_id="local-default", owner_user_id="guest-B"),
        lambda: service.list_all_messages(first.conversation_id, tenant_id="local-default", owner_user_id="guest-B"),
        lambda: service.get_message(first.conversation_id, tenant_id="local-default", owner_user_id="guest-B",
                                   message_id=answer.user_message.message_id),
        lambda: service.send_message(first.conversation_id, tenant_id="local-default", owner_user_id="guest-B",
                                    content="private question", idempotency_key="private-message"),
        lambda: service.delete_conversation(first.conversation_id, tenant_id="local-default", owner_user_id="guest-B"),
        lambda: repository.list_recent_completed_messages(
            first.conversation_id, "local-default", owner_user_id="guest-B", before_sequence_no=10, limit=10),
    )
    for check in checks:
        with pytest.raises(AgentConversationNotFoundError):
            check()


def test_private_conversation_can_use_one_public_source_and_replay_checks_source(build_service):
    service, _, retrieval, _, *_ = build_service()
    private = service.create_conversation(
        tenant_id="personal-tenant", owner_user_id="user-A", idempotency_key="private-conversation",
    )
    result = service.send_message(
        private.conversation_id, tenant_id="personal-tenant", source_tenant_id="local-default",
        owner_user_id="user-A", content="public course question", idempotency_key="source-message",
    )
    assert result.assistant_message.status == "COMPLETED"
    assert all(call[3] == "local-default" for call in retrieval.calls)
    assert result.assistant_message.source_tenant_id == "local-default"
    assert service.list_messages(private.conversation_id, tenant_id="personal-tenant", owner_user_id="user-A")
    with pytest.raises(AgentConflictError, match="different request fields"):
        service.send_message(
            private.conversation_id, tenant_id="personal-tenant", source_tenant_id="another-source",
            owner_user_id="user-A", content="public course question", idempotency_key="source-message",
        )
