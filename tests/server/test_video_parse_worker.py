from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace
from uuid import uuid4

from agent.video_parse_v2.input import GenerativeModelConfig
from engine.parsev2.schemas import ActionEvent, VideoParseResult
from engine.parsev2.validation import IntervalRefinementProposal
from gateway.config import load_config
from gateway.embedding import EmbeddingResult

from server.config.parse_profile import default_video_parse_v2_config
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.dto.jobDTO import PrefixParseLease, VideoParseLease
from server.model.job import (
    ChunkParseContentRecord,
    ChunkParseTaskRecord,
    IntervalParseTaskRecord,
    VideoParseTaskRecord,
)
from server.model.video import SeriesRecord, VideoRecord
from server.mq.video_parse_consumer import (
    VideoParseConsumer,
    VideoParseConsumerHandlers,
)
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository.video_parse import VideoParseRepository
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_parse_indexing import (
    VideoParseIndexingContract,
    VideoParseIndexingService,
)
from server.service.video_parse_media import MediaLeaseIdentity, VideoParseMediaService
from server.storage.elasticsearch import StoredDocumentState


class _ArtifactResolver:
    @staticmethod
    def resolve(uri: str) -> Path:
        return Path(uri)


class _EmbeddingClient:
    def __init__(self, model: str) -> None:
        self.model = model
        self.text_calls = 0
        self.video_calls = 0

    def embed_text(self, _text: str) -> EmbeddingResult:
        self.text_calls += 1
        return EmbeddingResult(
            model=self.model,
            modality="text",
            vector=[1.0, 0.0],
        )

    def embed_video(self, _video_path: str | Path) -> EmbeddingResult:
        self.video_calls += 1
        return EmbeddingResult(
            model=self.model,
            modality="video",
            vector=[0.0, 1.0],
        )


class _ElasticsearchBoundary:
    semantic_dimensions = 2
    vision_dimensions = 2

    def __init__(self) -> None:
        self.documents = {}
        self.bulk_calls = 0

    def is_ready(self, documents) -> bool:
        return all(
            self.documents.get(document.document_id) == document
            for document in documents
        )

    def inspect(self, keys):
        return {
            key: StoredDocumentState(present=False)
            for key in dict.fromkeys(keys)
        }

    def bulk_upsert(self, documents, *, refresh=False):
        assert refresh == "wait_for"
        self.bulk_calls += 1
        for document in documents:
            self.documents[document.document_id] = document
        return [document.document_id for document in documents]

    def verify_ready(self, documents) -> bool:
        return self.is_ready(documents)


def _write_media(command) -> None:
    Path(command[-1]).write_bytes(b"prepared-media")


def _parse_config() -> dict:
    config = default_video_parse_v2_config()
    models = config.models.model_copy(update={
        "asr": config.models.asr.model_copy(update={"mode": "none"}),
        "embedding": config.models.embedding.model_copy(update={"dimensions": 2}),
    })
    return config.model_copy(update={
        "chunk_duration": 10,
        "interval_duration": 30,
        "batch_max_size": 2,
        "models": models,
    }).model_dump(mode="json")


def _create_task(factory, source: Path) -> str:
    now = datetime.now(UTC) - timedelta(seconds=1)
    series_id = str(uuid4())
    video_id = str(uuid4())
    task_id = str(uuid4())
    with factory.begin() as session:
        session.add(SeriesRecord(
            series_id=series_id,
            workspace_id="workspace-1",
            name="Worker fixture",
            description=None,
            status="INDEXING",
            important_extra_data=None,
            character_bible=[],
        ))
        session.add(VideoRecord(
            video_id=video_id,
            series_id=series_id,
            episode_order=1,
            upload_sequence=1,
            original_filename="episode.mp4",
            original_source_uri=str(source),
            content_hash="a" * 64,
            total_bytes=source.stat().st_size,
            duration_seconds=10,
            upload_status="COMPLETED",
            parse_status="QUEUED",
            upload_progress=100,
            parse_progress=0,
        ))
        session.add(VideoParseTaskRecord(
            id=task_id,
            video_id=video_id,
            pipeline_version="v2",
            status="PENDING",
            parse_config=_parse_config(),
            chunk_count=1,
            version=1,
            next_execute_time=now,
            attempt_count=0,
            max_attempts=3,
            lease_version=0,
            processing_duration_ms=0,
            processing_timing_complete=True,
            is_test=True,
            is_deleted=False,
            detail={"_worker_runtime_v2": {"accepted": True}},
            create_time=now,
            update_time=now,
        ))
    return task_id


def test_worker_runs_real_agent_repository_and_stage_commits(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"source-video")
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'worker.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    factory = create_session_factory(engine)
    task_id = _create_task(factory, source)
    repository = VideoParseRepository(factory)
    queue = MySqlVideoParseQueue(factory)
    media = VideoParseMediaService(
        tmp_path / "media-work",
        command_runner=_write_media,
    )
    store = _ElasticsearchBoundary()
    embedding = _EmbeddingClient("doubao-embedding-vision-250615")
    runtime = VideoParseRuntimeService(
        repository,
        queue,
        _ArtifactResolver(),
        media,
        load_config(),
        store,  # type: ignore[arg-type]
        worker_id="component-worker",
        lease_duration=timedelta(seconds=5),
        heartbeat_interval=timedelta(seconds=1),
    )

    async def generate_multimodal(
        _self,
        _binding,
        _inputs,
        **_options,
    ) -> VideoParseResult:
        return VideoParseResult(
            persons=[],
            timeline=[ActionEvent(
                event_type="ACTION",
                start_seconds=1,
                end_seconds=4,
                description="A hand waves in the frame.",
                participants=[],
                visible_persons=[],
            )],
        )

    async def generate_text(
        _self,
        _binding,
        _prompt,
        **_options,
    ) -> IntervalRefinementProposal:
        return IntervalRefinementProposal(updates=[])

    def build_indexing(_self, inputs, *, check_active):
        model = inputs.parse_config.models.embedding
        client = SimpleNamespace(close=lambda: None)
        service = VideoParseIndexingService(
            repository,
            store,  # type: ignore[arg-type]
            embedding,
            media_resolver=_ArtifactResolver.resolve,
            media_service=media,
            check_active=check_active,
            contract=VideoParseIndexingContract(
                retrieval_contract_id="parse-v2-retrieval-v1",
                semantic_provider="ark",
                semantic_model=model.model,
                semantic_encoder_revision="test-revision",
                semantic_dimensions=2,
                vision_provider="ark",
                vision_model=model.model,
                vision_encoder_revision="test-revision",
                vision_dimensions=2,
                vision_fps=model.video_fps,
                vision_max_height=inputs.parse_config.visual_input.max_height,
            ),
        )
        return service, client

    monkeypatch.setattr(
        runtime,
        "_generate_multimodal",
        MethodType(generate_multimodal, runtime),
    )
    monkeypatch.setattr(
        runtime,
        "_generate_text_structured",
        MethodType(generate_text, runtime),
    )
    monkeypatch.setattr(
        runtime,
        "_build_indexing_service",
        MethodType(build_indexing, runtime),
    )

    consumer = VideoParseConsumer(
        queue,
        VideoParseConsumerHandlers(
            prepare=runtime.prepare,
            understand=runtime.understand,
            refine=runtime.refine,
            index=runtime.index,
            prefix=runtime.run_next_prefix,
        ),
        worker_id="component-worker",
        lease_duration=timedelta(seconds=5),
        heartbeat_interval=timedelta(seconds=1),
        retry_initial_delay=timedelta(milliseconds=10),
        retry_max_delay=timedelta(milliseconds=100),
        poll_interval=timedelta(milliseconds=10),
        max_concurrent_handlers=3,
    )
    consumer.start()
    deadline = time.monotonic() + 8
    try:
        while time.monotonic() < deadline:
            with factory() as session:
                if session.get(VideoParseTaskRecord, task_id).status == "COMPLETE":
                    break
            time.sleep(0.02)
        else:
            with factory() as session:
                task = session.get(VideoParseTaskRecord, task_id)
                intervals = session.query(IntervalParseTaskRecord).all()
                raise AssertionError(
                    "V2 Worker did not complete: "
                    f"consumer_failure={consumer.failure!r}, "
                    f"task_status={task.status!r}, "
                    f"task_error={task.last_error!r}, "
                    f"intervals={[(item.status, item.last_error) for item in intervals]!r}"
                )
    finally:
        consumer.close(timeout=2)
        engine.dispose()

    assert consumer.failure is None
    with factory() as session:
        task = session.get(VideoParseTaskRecord, task_id)
        chunks = session.query(ChunkParseTaskRecord).all()
        intervals = session.query(IntervalParseTaskRecord).all()
        contents = session.query(ChunkParseContentRecord).all()
        assert task.status == "COMPLETE"
        assert task.lease_owner is None
        assert [item.status for item in chunks] == ["COMPLETE"]
        assert [item.status for item in intervals] == ["COMPLETE"]
        assert len(contents) == 1
        assert contents[0].detail["scope"] == [0]
        assert contents[0].detail["_index_embeddings_v2"]["content_version"] == 1
    assert embedding.text_calls == 1
    assert embedding.video_calls == 1
    assert store.bulk_calls == 1
    assert len(store.documents) == 1
    assert list(media.base_directory.rglob(".lease.json")) == []


def test_consumer_async_tick_is_executable_in_isolation():
    """Keep the public async diagnostic surface explicit for event-loop callers."""

    assert asyncio.iscoroutinefunction(VideoParseConsumer.run_once)


def test_prefix_agent_factory_receives_frozen_fallback_controls(monkeypatch):
    captured = {}

    class _Model:
        @staticmethod
        def run_structured(*_args, **_kwargs):
            return IntervalRefinementProposal(updates=[])

    class _Factory:
        def __init__(self, config, _sources):
            captured["config"] = config

        @staticmethod
        def build_structured(**_kwargs):
            return _Model()

        @staticmethod
        def close():
            captured["closed"] = True

    monkeypatch.setattr("server.service.video_parse.AgentModelFactory", _Factory)
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = load_config()
    binding = GenerativeModelConfig(
        source="deepseek_api",
        model="primary-model",
        request_timeout_seconds=61,
        max_output_tokens=4096,
        reasoning_effort="none",
        fallback={
            "source": "ark_default",
            "model": "fallback-model",
            "request_timeout_seconds": 17,
            "max_output_tokens": 2048,
            "reasoning_effort": "none",
        },
    )

    result = asyncio.run(runtime._generate_text_structured(
        binding,
        "refine",
        tools=(object(),),  # type: ignore[arg-type]
        result_type=IntervalRefinementProposal,
        max_model_calls=2,
    ))

    assert result.updates == []
    fallback = captured["config"].fallback
    assert fallback.request_timeout_seconds == 17
    assert fallback.max_output_tokens == 2048
    assert fallback.reasoning_effort == "none"
    assert captured["closed"] is True


def test_prefix_cancellation_releases_lease_without_retrying(monkeypatch):
    released = []
    retried = []

    class _Repository:
        @staticmethod
        def load_task_input(_task_id):
            return object()

        @staticmethod
        def release_prefix(lease):
            released.append(lease)
            return True

        @staticmethod
        def retry_prefix(*args, **kwargs):
            retried.append((args, kwargs))
            return True

    started = asyncio.Event()

    class _PrefixAgent:
        @staticmethod
        async def run(_task_id):
            started.set()
            await asyncio.Future()

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._repository = _Repository()
    runtime._prefix_lease = ContextVar("test_prefix_lease", default=None)
    runtime.agent = SimpleNamespace(prefix_context_agent=_PrefixAgent())

    @asynccontextmanager
    async def model_stage(_self, *_args, **_kwargs):
        yield object()

    monkeypatch.setattr(
        runtime,
        "_model_stage",
        MethodType(model_stage, runtime),
    )
    lease = PrefixParseLease(
        video_parse_task_id=str(uuid4()),
        chunk_parse_task_id=str(uuid4()),
        video_chunk_id=str(uuid4()),
        sequence_no=0,
        worker_id="prefix-worker",
        lease_version=2,
        lease_expire_time=datetime.now(UTC) + timedelta(minutes=1),
        retry_count=1,
        content_version=3,
        content={},
        content_detail={},
        series_prefix_compact="",
        video_prefix_compact="",
    )

    async def execute():
        task = asyncio.create_task(runtime._execute_prefix(lease))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("Prefix cancellation was not propagated")

    asyncio.run(execute())
    assert released == [lease]
    assert retried == []


def test_model_stage_accepts_post_commit_heartbeat_loss(monkeypatch):
    managers = []

    class _Manager:
        def __init__(self, *_args, **_kwargs):
            self.cancel_count = 0
            managers.append(self)

        @contextmanager
        def capture(self):
            yield

        @staticmethod
        def heartbeat():
            raise RuntimeError("stage lease was cleared by commit")

        def cancel(self):
            self.cancel_count += 1

    class _Queue:
        def __init__(self):
            self.checked = 0

        def is_execution_lease_committed(self, _lease):
            self.checked += 1
            return True

    monkeypatch.setattr(
        "server.service.video_parse.VideoParseModelCallManager",
        _Manager,
    )
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._repository = object()
    runtime._queue = _Queue()
    runtime._lease_duration = timedelta(seconds=1)
    runtime._heartbeat_interval = timedelta(milliseconds=1)
    runtime._parse_usage = None
    runtime._model_manager = ContextVar("test_model_manager", default=None)
    lease = VideoParseLease(
        video_parse_task_id=str(uuid4()),
        video_id=str(uuid4()),
        worker_id="worker",
        lease_version=1,
        lease_expire_time=datetime.now(UTC) + timedelta(seconds=1),
        attempt_count=1,
        max_attempts=3,
        parse_config={},
    )

    async def execute():
        async with runtime._model_stage(
            lease,
            None,  # type: ignore[arg-type]
            sequence_no=None,
            category="parse",
            check_active=lambda: None,
        ):
            await asyncio.sleep(0.01)

    asyncio.run(execute())
    assert runtime._queue.checked >= 1
    assert managers[0].cancel_count == 1


def test_media_reaper_uses_exact_queue_identity_and_fails_closed():
    identity = MediaLeaseIdentity(
        video_parse_task_id=str(uuid4()),
        execution_task_id=str(uuid4()),
        lease_version=3,
        execution_kind="interval",
    )
    captured = {}

    class _Media:
        @staticmethod
        def reap_stale(*, older_than_seconds, is_active):
            captured["older_than_seconds"] = older_than_seconds
            captured["active"] = is_active(identity)
            return 0

    class _Queue:
        @staticmethod
        def is_execution_lease_active(*_args, **_kwargs):
            raise RuntimeError("database unavailable")

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._queue = _Queue()
    runtime._media_service = _Media()
    runtime._lease_duration = timedelta(seconds=5)

    assert asyncio.run(runtime.reap_stale_media()) == 0
    assert captured == {
        "older_than_seconds": 10.0,
        "active": True,
    }
