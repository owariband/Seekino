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

from agent.chat.common.errors import AgentContextBudgetError, AgentModelInvocationError
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
):
    return service.send_message(
        conversation.conversation_id, tenant_id="local-default",
        content=content, idempotency_key=key, series_ids=series_ids,
        reference=reference, extra_context=extra_context,
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

    def run_agent_loop(self, prompt, *, tools, handle_text, check_control, **_kwargs):
        self.prompts.append(prompt)
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
        assert result["result_count"] > 0
        assert result["answer_preview"]
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


@pytest.mark.parametrize("playback_change", [
    None, "position", "series", "tenant", "negative_position", "float_position", "bool_position",
])
def test_current_video_scope_is_trusted_without_making_unready_series_retrievable(
    build_service, playback_change,
):
    class CurrentVideoModel:
        model_name = "current-video-test"
        supports_vision_input = False

        def run_agent_loop(self, _prompt, *, tools, handle_text, **_kwargs):
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
    class CurrentVideoModel:
        model_name = "current-video-test"

        def run_agent_loop(self, _prompt, *, tools, handle_text, **_kwargs):
            by_name = {tool.name: tool for tool in tools}
            by_name["search_video_graph"].invoke({"query": "what was explained"})
            handle_text("Answer from the available evidence.")

    service, conversation, retrieval, repository, *_ = build_service(
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
        for block in source.assistant_message.search_result["renderBlocks"]
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
        assert stored.detail == {"timing": {
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
    service, conversation, _, _, _, _, _ = build_service()
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
    assert completed.search_result["retrievalTrace"] == [{
        "query": "第一次改写",
        "topK": service.default_top_k,
        "strategyVersion": "query-adaptive-three-channel-rrf-v2",
        "requestedWeights": {"lexical": 0.1, "semantic": 0.5, "visual": 0.4},
        "effectiveWeights": {"lexical": 0.1, "semantic": 0.5, "visual": 0.4},
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
