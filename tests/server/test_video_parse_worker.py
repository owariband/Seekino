from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from agent.video_parse_v2.input import GenerativeModelConfig, ParseInput
from agent.video_parse_v2.state import ChunkExecution, LeaseToken
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseContextCanvas
from engine.common.model_factory import build_asr_agent
from engine.common.transcription import TranscriptAnalysis
from engine.parsev2.observations import OCRResult
from engine.parsev2.schemas import ActionEvent, VideoParseResult
from engine.parsev2.validation import IntervalRefinementProposal
from engine.rag import (
    ChunkIndexingContract,
    EncoderContract,
    VideoClipInputContract,
    VIDEO_CLIP_PREPROCESS_REVISION,
)
from engine.rag.encoding import EmbeddingRequest, SparseEmbeddingRequest, accept_sparse_result
from gateway.config import ArkModelSourceConfig, OpenAIAudioSourceConfig, load_config
from gateway.ark.embedding import DOUBAO_MULTIMODAL_EMBEDDING_REVISION
from gateway.embedding import EmbeddingResult
from gateway.usage import TokenUsage, capture_model_usage, model_call_context, track_model_call

from server.api.app import create_app
from server.bootstrap import ApplicationServices
from server.config.parse_profile import default_video_parse_v2_config
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.dto.jobDTO import PrefixParseLease, VideoParseLease
from server.model.job import (
    ChunkParseContent,
    ChunkParseTask,
    IntervalParseTask,
    VideoParseTask,
)
from server.model.upload import UPLOAD_DELETION_PENDING_ERROR_CODE, UploadSession
from server.model.parse_usage import ModelUsage
from server.model.video import Series, Video
from server.mq.video_parse_consumer import (
    VideoParseConsumer,
    VideoParseConsumerHandlers,
)
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository.video_parse import RUNTIME_DETAIL_KEY, VideoParseRepository
from server.repository.job import ParseJobRepository
from server.repository.library import LibraryRepository
from server.repository.model_usage import ModelUsageRepository
from server.repository.upload import UploadRepository
from server.service.job import ParseJobService
from server.service.library import LibraryService
from server.service.media import MediaService
from server.service.parse_usage import ParseUsageService
from server.service.upload import UploadService, UploadServiceConfig
from server.service.video_retrieval import VideoParseRetrievalService
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_parse_index_coordinator import (
    VideoParseIndexCoordinator,
    VideoParseIndexCoordinationError,
    _accept_result,
)
from server.service.video_parse_media import (
    MediaLeaseIdentity,
    PreparedVideoParseMedia,
    VideoParseMediaService,
)
from server.storage.artifact import LocalArtifactStore
from server.storage.elasticsearch import (
    ElasticsearchVideoChunkStore,
    StoredDocumentState,
    VideoChunkIndexMatch,
    document_id,
)
from server.utils.time import datetime_to_unix_time_ms


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
            self.documents.get(
                document_id(document.parse_task_id, document.video_chunk_id)
            ) == document
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
            physical_id = document_id(
                document.parse_task_id,
                document.video_chunk_id,
            )
            self.documents[physical_id] = document
        return [
            document_id(document.parse_task_id, document.video_chunk_id)
            for document in documents
        ]

    def verify_ready(self, documents) -> bool:
        return self.is_ready(documents)


class _RecoveringElasticsearchBoundary(_ElasticsearchBoundary):
    """Local I/O boundary, not a proof of Elasticsearch query or race semantics."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_bulk = True
        self.fail_next_delete = False
        self.bulk_keys = []

    def bulk_upsert(self, documents, *, refresh=False):
        self.bulk_keys.append([
            (item.parse_task_id, item.video_chunk_id) for item in documents
        ])
        if self.fail_next_bulk:
            self.fail_next_bulk = False
            super().bulk_upsert(documents[:1], refresh=refresh)
            raise RuntimeError("injected partial indexing failure")
        return super().bulk_upsert(documents, refresh=refresh)

    def inspect(self, keys):
        states = {}
        for key in keys:
            document = self.documents.get(document_id(*key))
            if document is None:
                states[key] = StoredDocumentState(present=False)
                continue
            states[key] = StoredDocumentState(
                **{
                    name: getattr(document, name)
                    for name in StoredDocumentState.__dataclass_fields__
                    if hasattr(document, name)
                },
                present=True,
                lexical_fields_present=True,
                semantic_text_present=True,
                semantic_vector_present=bool(document.semantic_vector),
                vision_vector_present=bool(document.vision_vector),
            )
        return states

    def inspect_exact(self, documents):
        return {
            document_id(item.parse_task_id, item.video_chunk_id): self.is_ready([item])
            for item in documents
        }

    def recall(self, request, *, exact=False):
        return ElasticsearchVideoChunkStore.recall(self, request, exact=exact)

    def search_lexical(self, query, *, limit, **scope):
        documents = [
            item for item in self.documents.values()
            if item.tenant_id == scope["tenant_id"]
            and item.series_id in scope["series_ids"]
            and scope["parse_tasks_by_video_id"].get(item.video_id) == item.parse_task_id
            and item.index_generation == scope["index_generation"]
            and any(query in field.raw_text for field in item.projection.lexical_fields)
        ]
        return [
            VideoChunkIndexMatch(
                rank=rank,
                score=1.0,
                matched_fields=tuple(
                    field for field in item.projection.lexical_fields if query in field.raw_text
                ),
                **{
                    name: getattr(item, name)
                    for name in VideoChunkIndexMatch.__dataclass_fields__
                    if hasattr(item, name)
                },
            )
            for rank, item in enumerate(documents[:limit], 1)
        ]

    def bulk_delete(self, keys, *, refresh=False):
        assert refresh == "wait_for"
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("injected index cleanup failure")
        for key in keys:
            self.documents.pop(document_id(*key), None)

    def verify_absent(self, keys):
        return all(document_id(*key) not in self.documents for key in keys)


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
        session.add(Series(
            id=series_id,
            tenant_id="workspace-1",
            name="Worker fixture",
            description=None,
            status="INDEXING",
            important_extra_data=None,
            character_bible=[],
        ))
        session.add(Video(
            id=video_id,
            series_id=series_id,
            episode_order=1,
            upload_sequence=1,
            filename="episode.mp4",
            source_uri=str(source),
            hash="a" * 64,
            size_bytes=source.stat().st_size,
            duration_ms=10_000,
            upload_status="COMPLETED",
            parse_status="QUEUED",
            upload_progress=100,
            parse_progress=0,
        ))
        session.add(VideoParseTask(
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
            create_time=datetime_to_unix_time_ms(now),
            update_time=datetime_to_unix_time_ms(now),
        ))
    return task_id


@pytest.mark.parametrize("adapter", ["openai_audio", "ark", "none"])
def test_transcription_uses_frozen_source_prepared_media_and_absolute_milliseconds(
    monkeypatch, tmp_path, adapter,
):
    source = (
        OpenAIAudioSourceConfig(
            provider="speech", base_url="https://speech.example/v1",
            api_key_env="TEST_ASR_KEY",
        )
        if adapter == "openai_audio"
        else ArkModelSourceConfig(api_key_env="TEST_ASR_KEY")
    )
    monkeypatch.setenv("TEST_ASR_KEY", "test-key")
    monkeypatch.delenv("UNUSED_ASR_KEY", raising=False)
    gateway = load_config()
    gateway = gateway.model_copy(update={
        "model_sources": {
            **gateway.model_sources,
            "frozen_asr": source,
            "configured_asr": ArkModelSourceConfig(api_key_env="UNUSED_ASR_KEY"),
        },
        "video_parser": gateway.video_parser.model_copy(update={
            "transcription_source": "configured_asr",
        }),
    })
    config = _parse_config()
    config["models"]["asr"].update(
        source="frozen_asr", model="frozen-speech", fps=0.5,
        mode="none" if adapter == "none" else "lite",
        request_timeout_seconds=45,
        max_output_tokens=2048 if adapter == "ark" else None,
    )
    inputs = ParseInput.model_validate({
        "series_id": "series", "video_id": "video", "parse_config": config,
    })
    execution = ChunkExecution(
        chunk_parse_task_id="chunk-task", video_parse_task_id="video-task",
        sequence_no=1, chunk_start_seconds=13_000, chunk_end_seconds=14_000,
        allowed_start_seconds=12_000, allowed_end_seconds=15_000,
        inputs=inputs,
        lease=LeaseToken("worker", 1, datetime.now(UTC) + timedelta(seconds=30)),
    )
    video = tmp_path / "prepared.mp4"
    video.write_bytes(b"video")
    audio = tmp_path / "prepared.wav"
    audio.write_bytes(b"audio")
    media = PreparedVideoParseMedia(video, audio, ())
    runtime = VideoParseRuntimeService(
        object(), object(), _ArtifactResolver(),
        VideoParseMediaService(tmp_path / "media"), gateway, _ElasticsearchBoundary(),
        worker_id="worker", lease_duration=timedelta(seconds=30),
        heartbeat_interval=timedelta(seconds=5),
    )
    calls = []

    async def prepared_media(_self, loaded):
        assert loaded is execution
        return media

    def transcribe(path, **kwargs):
        assert adapter == "openai_audio"
        assert path == media.audio_path
        calls.append(("audio", kwargs))
        return {"segments": [
            {"start": 1.5, "end": 4, "text": "hello"},
            {"start": 3, "end": 4, "text": "outside"},
        ]}

    def build_model(model, **kwargs):
        assert adapter == "ark"
        calls.append((model, kwargs))

        def generate(request_inputs, *, schema_name, result_type):
            assert request_inputs[0].path == media.video_path
            assert request_inputs[0].fps == 0.5
            assert schema_name == "candidate_transcript"
            assert result_type is not TranscriptAnalysis
            return result_type.model_validate({"transcript": [
                {"start_seconds": 1.5, "end_seconds": 4, "text": "hello"},
                {"start_seconds": 3, "end_seconds": 4, "text": "outside"},
            ]})

        return SimpleNamespace(model_name=model, generate=generate)

    def build_agent(bound_config, **kwargs):
        assert adapter == "ark"
        return build_asr_agent(bound_config, ark_model_type=build_model, **kwargs)

    monkeypatch.setattr(runtime, "_ensure_chunk_media", MethodType(prepared_media, runtime))
    monkeypatch.setattr("server.service.video_parse.transcribe_audio", transcribe)
    monkeypatch.setattr("server.service.video_parse.build_asr_agent", build_agent)
    result = asyncio.run(runtime.transcribe_chunk(execution))

    assert result == {"transcript": [] if adapter == "none" else [{
        "start_seconds": 13_500, "end_seconds": 15_000, "text": "hello",
    }]}
    if adapter == "openai_audio":
        assert calls == [("audio", {
            "model": "frozen-speech", "provider": "speech",
            "base_url": "https://speech.example/v1", "api_key": "test-key",
            "timeout": 45, "response_format": "verbose_json",
        })]
    elif adapter == "ark":
        assert calls == [("frozen-speech", {
            "request_timeout_seconds": 45, "max_output_tokens": 2048,
            "api_key": "test-key",
        })]
    else:
        assert calls == []


def test_ocr_host_uses_agent_prompt_and_shared_engine_result(monkeypatch, tmp_path):
    config = _parse_config()
    config["ocr_enabled"] = True
    config["models"]["ocr"] = dict(config["models"]["understanding"])
    inputs = ParseInput.model_validate({
        "series_id": "series", "video_id": "video", "parse_config": config,
    })
    execution = ChunkExecution(
        "chunk-task", "video-task", 0, 0, 20_000, 0, 20_000, inputs,
        LeaseToken("worker", 1, datetime.now(UTC) + timedelta(seconds=30)),
    )
    media = PreparedVideoParseMedia(
        tmp_path / "prepared.mp4", tmp_path / "prepared.wav",
        ((1000, tmp_path / "frame-1.jpg"), (2000, tmp_path / "frame-2.jpg")),
    )
    runtime = VideoParseRuntimeService(
        object(), object(), _ArtifactResolver(),
        VideoParseMediaService(tmp_path / "media"), load_config(), _ElasticsearchBoundary(),
        worker_id="worker", lease_duration=timedelta(seconds=30),
        heartbeat_interval=timedelta(seconds=5),
    )
    expected = OCRResult.model_validate({"lines": [{
        "start_seconds": 1000, "end_seconds": 2000, "text": "営業中", "language": "ja",
    }]})

    async def prepared_media(_self, loaded):
        assert loaded is execution
        return media

    async def generate(_self, binding, request_inputs, *, schema_name, result_type):
        assert binding is inputs.parse_config.models.ocr
        assert schema_name == "video_parse_v2_ocr"
        assert result_type is OCRResult
        assert [item.path for item in request_inputs[:-1]] == [
            tmp_path / "frame-1.jpg", tmp_path / "frame-2.jpg",
        ]
        assert request_inputs[-1].text == (
            "Read only visible non-dialogue on-screen text from the ordered frames. "
            "Use absolute integer source-video milliseconds inside the authorized range. "
            "Frame timestamps: [1000, 2000]."
        )
        return expected

    monkeypatch.setattr(runtime, "_ensure_chunk_media", MethodType(prepared_media, runtime))
    monkeypatch.setattr(runtime, "_generate_multimodal", MethodType(generate, runtime))
    assert asyncio.run(runtime.read_chunk_text(execution)) is expected


@pytest.mark.parametrize(
    ("route", "encoder_model", "expected_model"),
    [
        ("doubao-embedding-vision-250615", None, "doubao-embedding-vision-250615"),
        ("ep-A", None, "doubao-embedding-vision-250615"),
        ("ep-A", "doubao-embedding-vision-251215", "doubao-embedding-vision-251215"),
    ],
)
def test_index_builder_binds_frozen_encoder_and_preserves_embedding_route(
    monkeypatch,
    tmp_path,
    route,
    encoder_model,
    expected_model,
):
    monkeypatch.setenv("ARK_API_KEY", "test-ark-key")
    config = _parse_config()
    embedding = config["models"]["embedding"]
    embedding["model"] = route
    embedding.pop("encoder_model", None)
    if encoder_model is not None:
        embedding["encoder_model"] = encoder_model
    inputs = ParseInput.model_validate({
        "series_id": "series", "video_id": "video", "parse_config": config,
    })
    inputs = ParseInput.model_validate_json(inputs.model_dump_json())
    assert inputs.parse_config.models.embedding.model == route
    assert inputs.parse_config.models.embedding.encoder_model == encoder_model
    requests = []

    class Client:
        model = expected_model
        closed = False

        def __init__(self):
            self.multimodal_embeddings = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(model=self.model, data=SimpleNamespace(
                embedding=[1.0, 0.0],
                sparse_embedding=[SimpleNamespace(index=7, value=0.5)],
            ))

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr("server.service.video_parse.Ark", SimpleNamespace(volc=lambda **_kwargs: client))
    runtime = VideoParseRuntimeService(
        object(), object(), _ArtifactResolver(),
        VideoParseMediaService(tmp_path / "media"),
        load_config(), _ElasticsearchBoundary(),
        worker_id="endpoint-worker", lease_duration=timedelta(seconds=30),
        heartbeat_interval=timedelta(seconds=5), sparse_enabled=True,
    )
    coordinator, selected_client = runtime._build_index_coordinator(inputs, check_active=lambda: None)
    try:
        assert selected_client is client
        assert coordinator._embedder.model_name == route
        assert coordinator.contract.semantic_encoder.model == expected_model
        assert coordinator.contract.vision_encoder.model == expected_model
        assert coordinator.contract.sparse_encoder.model == expected_model
        request = EmbeddingRequest(
            channel="semantic", encoder=coordinator.contract.semantic_encoder,
            semantic_text="index text",
        )
        result = coordinator._embedder.embed_text(request.semantic_text)
        assert result.model == expected_model
        assert _accept_result(request, result).vector == (1.0, 0.0)
        video = tmp_path / "video.mp4"
        video.write_bytes(b"prepared-video")
        assert coordinator._embedder.embed_video(video).model == expected_model
        sparse = coordinator._embedder.embed_sparse("sparse text")
        sparse_request = SparseEmbeddingRequest(encoder=coordinator.contract.sparse_encoder, text="sparse text")
        assert accept_sparse_result(sparse_request, model=sparse.model, weights=sparse.weights).token_weights == (("7", 0.5),)
        client.model = "wrong-encoder"
        wrong = coordinator._embedder.embed_text(request.semantic_text)
        assert wrong.model == "wrong-encoder"
        with pytest.raises(VideoParseIndexCoordinationError, match="response contract differs"):
            _accept_result(request, wrong)
        assert [item["model"] for item in requests] == [route] * 4
        assert requests[1]["input"][0]["video_url"]["fps"] == inputs.parse_config.models.embedding.video_fps
    finally:
        selected_client.close()
    assert client.closed


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
        probe_duration=lambda _path: 10_000,
    )
    store = _ElasticsearchBoundary()
    class ObservedEmbedding(_EmbeddingClient):
        def embed_text(self, text):
            with track_model_call(provider="fixture", model=self.model, operation="text_embedding") as tracker:
                tracker.usage = TokenUsage(3, 0, 3, None, None)
                return super().embed_text(text)

        def embed_video(self, video):
            with track_model_call(provider="fixture", model=self.model, operation="video_embedding") as tracker:
                tracker.usage = TokenUsage(4, 0, 4, None, None)
                return super().embed_video(video)

    embedding = ObservedEmbedding("doubao-embedding-vision-250615")
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
        parse_usage=ParseUsageService(ModelUsageRepository(factory)),
    )

    async def generate_multimodal(_self, _binding, _inputs, **options):
        with track_model_call(provider="fixture", model="vision", operation="video_parse") as tracker:
            tracker.usage = TokenUsage(10, 2, 12, None, None)
        canvas = options["canvas"]
        reference = canvas.items("members")[0][0]
        write = next(tool for tool in options["tools"] if tool.name == "write_context_canvas")
        write.invoke({
            "batch_id": "understanding", "expected_revision": canvas.revision,
            "operations": [{"op": "add_event", "member_ref": reference, "key": "wave",
                            "event": ActionEvent(event_type="ACTION", start_seconds=1000,
                                                 end_seconds=4000, description="A hand waves in the frame.",
                                                 participants=[], visible_persons=[]).model_dump(mode="json")}],
        })
        return CanvasCompletion(complete=True)

    async def generate_text(_self, _binding, _prompt, **_options):
        with track_model_call(provider="fixture", model="refine", operation="chat") as tracker:
            tracker.usage = TokenUsage(10, 2, 12, None, None)
        return CanvasCompletion(complete=True)

    def build_index_coordinator(_self, inputs, *, check_active):
        model = inputs.parse_config.models.embedding
        client = SimpleNamespace(close=lambda: None)
        encoder = EncoderContract(
            provider="ark",
            model=model.model,
            encoder_revision="test-revision",
            dimensions=2,
        )
        service = VideoParseIndexCoordinator(
            repository,
            store,  # type: ignore[arg-type]
            embedding,
            media_resolver=_ArtifactResolver.resolve,
            media_service=media,
            check_active=check_active,
            contract=ChunkIndexingContract(
                retrieval_contract_id="parse-v2-retrieval-v1",
                semantic_encoder=encoder,
                vision_encoder=encoder,
                visual_input_contract=VideoClipInputContract(
                    fps=model.video_fps,
                    max_height=inputs.parse_config.visual_input.max_height,
                    mime_type="video/mp4",
                    preprocess_revision=VIDEO_CLIP_PREPROCESS_REVISION,
                ),
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
        "_build_index_coordinator",
        MethodType(build_index_coordinator, runtime),
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
                if session.get(VideoParseTask, task_id).status == "COMPLETE":
                    break
            time.sleep(0.02)
        else:
            with factory() as session:
                task = session.get(VideoParseTask, task_id)
                intervals = session.query(IntervalParseTask).all()
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
        task = session.get(VideoParseTask, task_id)
        chunks = session.query(ChunkParseTask).all()
        intervals = session.query(IntervalParseTask).all()
        contents = session.query(ChunkParseContent).all()
        assert task.status == "COMPLETE"
        assert task.lease_owner is None
        assert [item.status for item in chunks] == ["COMPLETE"]
        assert [item.status for item in intervals] == ["COMPLETE"]
        assert len(contents) == 1
        assert contents[0].detail["scope"] == [0]
        index_detail = contents[0].detail["_index_embeddings_v2"]
        assert index_detail["content_version"] == 1
        assert index_detail["receipt"]["content_version"] == 1
        assert index_detail["receipt"]["index_generation"] == 1
        assert index_detail["receipt"]["parse_task_id"] == task_id
        rows = session.scalars(select(ModelUsage)).all()
        assert len(rows) == 4 and len({row.id for row in rows}) == 4
        assert all(row.task_type == "video_parse" and row.run_id == task_id for row in rows)
        assert sum(row.usage["tokens"]["total_tokens"] for row in rows) == 31
        assert {row.metadata_["context"]["stage"] for row in rows} == {"understand", "refine", "index"}
        model_calls = task.detail[RUNTIME_DETAIL_KEY]["model_calls"]
        assert set(model_calls["used_call_ids"]) == {row.id for row in rows}
        assert model_calls["active"] == {}
    assert embedding.text_calls == 1
    assert embedding.video_calls == 1
    assert store.bulk_calls == 1
    assert len(store.documents) == 1
    assert list(media.base_directory.rglob(".lease.json")) == []


def test_consumer_async_tick_is_executable_in_isolation():
    """Keep the public async diagnostic surface explicit for event-loop callers."""

    assert asyncio.iscoroutinefunction(VideoParseConsumer.run_once)


@pytest.mark.parametrize("interval_duration_seconds", [20, 30], ids=["full-window", "short-window"])
def test_uploaded_v2_pipeline_recovers_indexing_searches_receipt_and_reconciles_delete(
    monkeypatch, tmp_path, interval_duration_seconds,
):
    """Exercise real Server/Agent boundaries with SQLite and offline model/ES I/O."""

    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'product-pipeline.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    create_schema(engine)
    factory = create_session_factory(engine)
    artifacts = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    repository = VideoParseRepository(factory)
    queue = MySqlVideoParseQueue(factory)
    store = _RecoveringElasticsearchBoundary()
    media = VideoParseMediaService(
        tmp_path / "media", command_runner=_write_media,
        probe_duration=lambda _path: 10_000,
    )
    parse_config = _parse_config()
    parse_config["interval_duration"] = interval_duration_seconds
    parse_config["execution"]["max_retry_count"] = 0
    embedding = _EmbeddingClient(parse_config["models"]["embedding"]["model"])
    encoder = EncoderContract(
        provider="ark", model=embedding.model, dimensions=2,
        encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
    )
    uploads = UploadService(
        UploadRepository(factory), artifacts,
        config=UploadServiceConfig(
            preferred_segment_size_bytes=1024, maximum_upload_size_bytes=1024,
        ),
        probe_duration=lambda _path: 20_000,
        execution_mode="v2", video_parse_index_store=store,
        video_parse_query_encoder=encoder,
        video_parse_query_encoder_source=parse_config["models"]["embedding"]["source"],
    )
    library = LibraryService(LibraryRepository(factory, execution_mode="v2"), artifacts)
    retrieval = VideoParseRetrievalService(
        repository, store,
        SimpleNamespace(embed_query=lambda _query: pytest.fail("unexpected query encoding")),
        semantic_encoder=encoder, expected_index_generation=1,
        lexical_weight=1, text_weight=0, vision_weight=0,
    )
    services = ApplicationServices(
        library=library, uploads=uploads,
        jobs=ParseJobService(ParseJobRepository(factory), library, execution_mode="v2"),
        media=MediaService(library, artifacts), retrieval=retrieval,
        agent=SimpleNamespace(
            close=lambda: None,
            message_options=lambda: SimpleNamespace(
                top_k=SimpleNamespace(default_value=3, minimum=1, maximum=20),
            ),
        ),
    )
    runtime = VideoParseRuntimeService(
        repository, queue, artifacts, media, load_config(), store,
        worker_id="pipeline-worker", lease_duration=timedelta(seconds=10),
        heartbeat_interval=timedelta(seconds=1),
    )
    model_calls = {"understanding": 0, "ocr": 0, "refine": 0}

    async def generate_multimodal(_self, _binding, _inputs, **options):
        if options["schema_name"] == "video_parse_v2_ocr":
            model_calls["ocr"] += 1
            return options["result_type"](lines=[])
        model_calls["understanding"] += 1
        state = _self._require_chunk_binding().state
        assert state is not None
        canvas = options["canvas"]
        reference = canvas.items("members")[0][0]
        write = next(tool for tool in options["tools"] if tool.name == "write_context_canvas")
        write.invoke({
            "batch_id": "understanding", "expected_revision": canvas.revision,
            "operations": [{"op": "add_event", "member_ref": reference, "key": "wave",
                            "event": ActionEvent(event_type="ACTION", start_seconds=state.start_seconds + 1000,
                                                 end_seconds=state.start_seconds + 4000,
                                                 description="A hand waves in the frame.",
                                                 participants=[], visible_persons=[]).model_dump(mode="json")}],
        })
        return CanvasCompletion(complete=True)

    async def generate_text(_self, _binding, _prompt, **_options):
        model_calls["refine"] += 1
        return CanvasCompletion(complete=True)

    def build_index_coordinator(_self, inputs, *, check_active):
        return VideoParseIndexCoordinator(
            repository, store, embedding, media_resolver=artifacts.resolve,
            media_service=media, check_active=check_active,
            contract=ChunkIndexingContract(
                retrieval_contract_id="parse-v2-retrieval-v1",
                semantic_encoder=encoder, vision_encoder=encoder,
                visual_input_contract=VideoClipInputContract(
                    fps=inputs.parse_config.models.embedding.video_fps,
                    max_height=inputs.parse_config.visual_input.max_height,
                    mime_type="video/mp4", preprocess_revision=VIDEO_CLIP_PREPROCESS_REVISION,
                ),
            ),
        ), SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(runtime, "_generate_multimodal", MethodType(generate_multimodal, runtime))
    monkeypatch.setattr(runtime, "_generate_text_structured", MethodType(generate_text, runtime))
    monkeypatch.setattr(runtime, "_build_index_coordinator", MethodType(build_index_coordinator, runtime))
    consumer = VideoParseConsumer(
        queue, VideoParseConsumerHandlers(
            prepare=runtime.prepare, understand=runtime.understand,
            refine=runtime.refine, index=runtime.index,
        ),
        worker_id="pipeline-worker", lease_duration=timedelta(seconds=10),
        heartbeat_interval=timedelta(seconds=1), poll_interval=timedelta(milliseconds=10),
        retry_initial_delay=timedelta(milliseconds=10),
        # SQLite cannot enforce FOR UPDATE; true lock competition is a MySQL case.
        retry_max_delay=timedelta(milliseconds=100), max_concurrent_handlers=2,
    )
    try:
        with TestClient(create_app(services)) as client:
            series = client.post("/api/series/create", json={
                "tenantId": "pipeline-test", "name": "Product pipeline",
            })
            assert series.status_code == 201, series.text
            series_id = series.json()["seriesId"]
            created = client.post(
                "/api/uploads/create", params={"seriesId": series_id},
                headers={"Idempotency-Key": "pipeline-upload"},
                json={
                    "tenantId": "pipeline-test", "episodeOrder": 1,
                    "originalFilename": "episode.mp4", "mediaType": "video/mp4",
                    "totalSizeBytes": 4, "videoParseConfig": parse_config,
                },
            )
            assert created.status_code == 201, created.text
            upload_id = created.json()["uploadSessionId"]
            scope = {"tenantId": "pipeline-test", "uploadSessionId": upload_id}
            segment = client.put(
                "/api/uploads/segments/upload", params={**scope, "segmentNumber": 0},
                headers={
                    "Content-Range": "bytes 0-3/4",
                    "X-Content-SHA256": hashlib.sha256(b"abcd").hexdigest(),
                }, content=b"abcd",
            )
            assert segment.status_code == 200, segment.text
            completed = client.post("/api/uploads/complete", params=scope)
            assert completed.status_code == 200, completed.text
            task_id = completed.json()["videoParseTaskId"]
            video_id = completed.json()["videoId"]
            task_scope = {"tenantId": "pipeline-test", "videoParseTaskId": task_id}
            search_body = {
                "tenantId": "pipeline-test", "seriesIds": [series_id],
                "query": "hand", "topK": 5,
            }

            consumer.start()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                with factory() as session:
                    if session.get(VideoParseTask, task_id).status == "INDEXING_FAILED":
                        break
                time.sleep(0.02)
            else:
                pytest.fail(f"index failure was not persisted: {consumer.failure!r}")
            # Retry against the running service, on its existing event loop.
            assert consumer.running
            assert consumer.failure is None
            with factory() as session:
                task = session.get(VideoParseTask, task_id)
                assert task.status == "INDEXING_FAILED", task.last_error
                frozen_config = task.parse_config
                before_contents = [
                    (row.id, row.content, row.version, row.detail["scope"])
                    for row in session.query(ChunkParseContent).order_by(ChunkParseContent.id)
                ]
            assert len(store.documents) == 1
            assert len(store.bulk_keys[0]) == 2
            unavailable = client.post("/api/search", json=search_body)
            assert unavailable.status_code == 409, unavailable.text
            assert unavailable.json()["code"] == "SERIES_NOT_READY"
            failed = client.get("/api/jobs/video-parse/get", params=task_scope)
            assert failed.status_code == 200, failed.text
            assert failed.json()["canRetry"]
            assert "_worker_runtime_v2" not in failed.json()["detail"]
            assert client.get(
                "/api/jobs/video-parse/get", params={**task_scope, "tenantId": "other"},
            ).status_code == 404

            retried = client.post(
                "/api/uploads/retry-parse", params={**scope, "expectedJobVersion": 1},
            )
            assert retried.status_code == 200, retried.text
            assert retried.json()["videoParseTaskId"] == task_id
            assert retried.json()["version"] == 1
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                with factory() as session:
                    if session.get(VideoParseTask, task_id).status == "COMPLETE":
                        break
                time.sleep(0.02)
            else:
                pytest.fail(f"index recovery did not complete: {consumer.failure!r}")
            consumer.close(timeout=2)
            assert not consumer.running
            assert model_calls == {"understanding": 2, "ocr": 0, "refine": 1}
            assert (embedding.text_calls, embedding.video_calls) == (2, 2)
            assert len(store.bulk_keys) == 2
            assert len(store.bulk_keys[1]) == 1
            assert store.bulk_keys[1][0] in store.bulk_keys[0]
            with factory() as session:
                task = session.get(VideoParseTask, task_id)
                assert task.status == "COMPLETE"
                assert task.parse_config == frozen_config
                contents = session.query(ChunkParseContent).order_by(ChunkParseContent.id).all()
                assert [(row.id, row.content, row.version, row.detail["scope"]) for row in contents] == before_contents
                receipts = {
                    row.detail["_index_embeddings_v2"]["receipt"]["video_chunk_id"]:
                    row.detail["_index_embeddings_v2"]["receipt"] for row in contents
                }
            result = retrieval.search("hand", tenant_id="pipeline-test", series_ids=[series_id])
            assert len(result.hits) == 2
            assert result.searched_video_ids == [video_id]
            for hit in result.hits:
                receipt = receipts[hit.evidence.video_chunk_id]
                provenance = hit.evidence.parse_provenance
                assert receipt["parse_task_id"] == task_id
                assert provenance == {
                    "parseTaskId": receipt["parse_task_id"],
                    "contentVersion": receipt["content_version"],
                    "indexGeneration": receipt["index_generation"],
                }
            searched = client.post("/api/search", json=search_body)
            assert searched.status_code == 200, searched.text
            assert len(searched.json()["hits"]) == len(receipts)
            assert {hit["candidate"]["videoChunkId"] for hit in searched.json()["hits"]} == set(receipts)

            reparsed = client.post(
                "/api/uploads/reparse", params={**scope, "expectedJobVersion": 1},
            )
            assert reparsed.status_code == 200, reparsed.text
            assert reparsed.json()["videoParseTaskId"] != task_id
            assert reparsed.json()["version"] == 2
            assert len(store.documents) == 2
            superseded = client.post("/api/search", json=search_body)
            assert superseded.status_code == 409, superseded.text
            assert superseded.json()["code"] == "SERIES_NOT_READY"
            with factory() as session:
                assert session.get(VideoParseTask, task_id).status == "COMPLETE"

            # Cleanup failure must fence query access before external I/O recovers.
            store.fail_next_delete = True
            with pytest.raises(RuntimeError, match="injected index cleanup failure"):
                uploads.delete_session(upload_id, tenant_id="pipeline-test")
            with factory() as session:
                assert session.get(UploadSession, upload_id).last_error_code == UPLOAD_DELETION_PENDING_ERROR_CODE
                assert session.get(VideoParseTask, task_id).is_deleted
            assert len(store.documents) == 2
            fenced = client.post("/api/search", json=search_body)
            assert fenced.status_code == 409, fenced.text
            assert fenced.json()["code"] == "SERIES_NOT_READY"
            assert uploads.reconcile_deletions() == (1, 0)
            assert not store.documents
            deleted = client.post("/api/uploads/delete", params=scope)
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["status"] == "EXPIRED"
            assert deleted.json()["videoId"] is None
            assert deleted.json()["initialVideoParseTaskId"] is None
    finally:
        consumer.close(timeout=2)
        engine.dispose()
    assert consumer.failure is None
    assert list(media.base_directory.rglob(".lease.json")) == []


def test_canvas_tool_channel_receives_frozen_fallback_controls(monkeypatch):
    captured = {}
    closed = []

    class Primary:
        def __init__(self, model, **options):
            captured["primary"] = (model, options)

        def generate(self, inputs, **options):
            captured["generate"] = options
            return CanvasCompletion(complete=True)

        def close(self):
            closed.append("primary")

    class Fallback:
        def __init__(self, model, **options):
            captured["fallback"] = (model, options)

        def close(self):
            closed.append("fallback")

    monkeypatch.setattr("server.service.video_parse.DeepSeekMultimodalModel", Primary)
    monkeypatch.setattr("server.service.video_parse.ArkMultimodalModel", Fallback)
    monkeypatch.setenv("ARK_API_KEY", "fake-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = load_config()
    runtime._model_manager = ContextVar("fallback-controls-manager", default=None)
    binding = GenerativeModelConfig(
        source="deepseek_api", model="primary-model", request_timeout_seconds=61,
        max_output_tokens=4096, reasoning_effort="none",
        fallback={"source": "ark_default", "model": "fallback-model", "request_timeout_seconds": 17,
                  "max_output_tokens": 2048, "reasoning_effort": "none"},
    )
    result = asyncio.run(runtime._generate_text_structured(
        binding, "refine", tools=(object(),), result_type=CanvasCompletion, max_model_calls=2,
    ))
    assert result.complete
    assert captured["primary"][1]["request_timeout_seconds"] == 61
    assert captured["fallback"][1]["request_timeout_seconds"] == 17
    assert captured["fallback"][1]["max_output_tokens"] == 2048
    assert captured["fallback"][1]["reasoning_effort"] == "none"
    assert captured["generate"]["max_model_calls"] == 2
    assert captured["generate"]["context_window_policy"].output_reserve_tokens == 4096
    assert closed == ["fallback", "primary"]


@pytest.mark.parametrize("reasoning_effort", [None, "none", "low"])
def test_runtime_uses_frozen_ark_controls_instead_of_current_defaults(
    monkeypatch, reasoning_effort,
):
    captured = {}

    class _Model:
        def __init__(self, model, **kwargs):
            captured.update(model=model, **kwargs)

        @staticmethod
        def generate(*_args, **_kwargs):
            return IntervalRefinementProposal()

    monkeypatch.setenv("ARK_API_KEY", "fake-ark-key")
    monkeypatch.setattr("server.service.video_parse.ArkMultimodalModel", _Model)
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = load_config()
    binding = GenerativeModelConfig(
        source="ark_default", model="frozen-model", request_timeout_seconds=61,
        max_output_tokens=131072, reasoning_effort=reasoning_effort,
    )

    result = runtime._invoke_multimodal(
        binding, [], schema_name="refine", result_type=IntervalRefinementProposal,
    )

    assert result.updates == []
    assert captured["max_output_tokens"] == 131072
    assert captured["reasoning_effort"] == reasoning_effort
    assert captured["request_timeout_seconds"] == 61


def test_refinement_reuses_understanding_output_budget_and_reasoning(monkeypatch):
    from unittest.mock import AsyncMock

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._model_manager = ContextVar("budget-manager", default=None)
    generated = AsyncMock(return_value=CanvasCompletion(complete=True))
    monkeypatch.setattr(runtime, "_generate_text_structured", generated)
    config = default_video_parse_v2_config()
    assert config.models.refine is None

    asyncio.run(runtime.generate_interval_refinement(
        {"inputs": SimpleNamespace(parse_config=config)}, prompt="refine",
        canvas=ParseContextCanvas(scope="refine:budget", members={"member": {
            "sequence_no": 0, "version": 1, "bounds": (0, 20_000),
            "content": VideoParseResult(persons=[], timeline=[]),
        }}),
    ))

    binding = generated.await_args.args[0]
    assert binding.max_output_tokens == 65536
    assert binding.reasoning_effort == "none"
    assert config.models.asr.reasoning_effort is None


@pytest.mark.parametrize("maximum_tools", [0, 1])
def test_prefix_zero_and_single_tool_budgets_keep_canvas_facts(monkeypatch, maximum_tools):
    from agent.video_parse_v2.timelineEvent_canvas import PrefixCompactCompletion
    from agent.video_parse_v2.prompts import build_prefix_prompt
    from tests.agent.video_parse_v2.test_framework import _inputs, _lease, _result

    inputs = _inputs(compact_enabled=True)
    prefix = inputs.parse_config.prefix_context.model_copy(update={"max_tool_calls": maximum_tools})
    inputs = inputs.model_copy(update={"parse_config": inputs.parse_config.model_copy(update={"prefix_context": prefix})})
    context = {
        "chunk_parse_task_id": "member", "video_parse_task_id": "video", "inputs": inputs,
        "lease": _lease(), "sequence_no": 0, "allowed_start_seconds": 0, "allowed_end_seconds": 20_000,
        "scope": [0], "content": _result(), "content_version": 1,
        "series_prefix_compact": "", "video_prefix_compact": "",
        "evidence": {"asr": {"transcript": [{"start_seconds": 1000, "end_seconds": 3000, "text": "original"}]}, "ocr": None},
    }
    canvas = ParseContextCanvas.from_prefix(context)
    event_reference = canvas.items("events")[0][0]
    prompt = build_prefix_prompt(sequence_no=0, scope=[0], canvas=canvas, max_tool_calls=maximum_tools)
    assert event_reference in prompt and context["content"].timeline[0].description in prompt
    assert "original" in prompt
    calls = []

    async def generate(_binding, _prompt, *, tools, result_type, max_model_calls, **options):
        calls.append("generate")
        if maximum_tools == 0:
            assert not tools and result_type is PrefixCompactCompletion and max_model_calls == 1
            with pytest.raises(ValueError):
                result_type.model_validate({"complete": True, "series_compact": "summary", "video_compact": "summary", "timeline": []})
            return PrefixCompactCompletion(complete=True, series_compact="series", video_compact="video")
        assert result_type is CanvasCompletion and max_model_calls == inputs.parse_config.execution.max_model_calls
        write = next(tool for tool in tools if tool.name == "write_context_canvas")
        result = write.invoke({
            "batch_id": "one-batch", "expected_revision": canvas.revision,
            "operations": [
                {"op": "set_compact", "series_compact": "series", "video_compact": "video"},
                {"op": "patch_event", "event_ref": event_reference, "fields": {"description": "corrected in one permitted batch"}},
            ],
        })
        assert json.loads(result)["results"]
        return CanvasCompletion(complete=True)

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._model_manager = ContextVar("prefix-budget-manager", default=None)
    monkeypatch.setattr(runtime, "_generate_text_structured", generate)
    receipt = asyncio.run(runtime.generate_prefix_refinement(context, prompt=prompt, canvas=canvas))
    assert receipt.complete and calls == ["generate"]
    assert canvas.prefix_result().video_compact == "video"
    result = canvas.result("member")
    assert len(result.timeline) == 1
    assert result.timeline[0].start_seconds == 1000 and result.timeline[0].end_seconds == 3000
    assert result.timeline[0].description == ("corrected in one permitted batch" if maximum_tools else context["content"].timeline[0].description)
    assert canvas.get("evidence", "member") == context["evidence"]


@pytest.mark.parametrize("stage", ["refine", "prefix"])
@pytest.mark.parametrize("scope", [None, [], "0", [0, None], [False], [-1, 0], [0, 2]])
def test_canvas_input_rejects_invalid_persisted_scope_before_deriving_bounds(monkeypatch, stage, scope):
    from tests.agent.video_parse_v2.test_framework import _inputs, _result

    inputs = _inputs(compact_enabled=True)
    task_input = SimpleNamespace(task=SimpleNamespace(chunk_count=2), duration_seconds=40_000)
    member = SimpleNamespace(chunk_parse_task_id="member", sequence_no=0, detail={"scope": scope},
                             content=_result().model_dump(mode="json"), version=1)
    snapshot = SimpleNamespace(start_sequence_no=0, chunk_count=1, members=[member])
    lease = SimpleNamespace(chunk_parse_task_id="member", video_parse_task_id="video", sequence_no=0)
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._repository = SimpleNamespace(
        load_task_input=lambda _task: task_input,
        load_interval_members=lambda _lease: snapshot,
        load_prefix_content=lambda _lease: member,
    )
    runtime._prefix_lease = ContextVar("invalid-prefix-scope", default=lease)
    monkeypatch.setattr(runtime, "_require_interval_lease", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(runtime, "_parse_input", lambda _task: inputs)

    load = runtime.load_refine_input if stage == "refine" else runtime.load_prefix_context
    with pytest.raises(ValueError, match="scope"):
        asyncio.run(load("member"))


@pytest.mark.parametrize("max_output_tokens", [None, 65_536, 131_072])
def test_canvas_channel_preserves_output_budget_and_uses_valid_policy(monkeypatch, max_output_tokens):
    captured = {}

    class Model:
        def __init__(self, _name, **options):
            captured["constructed"] = options

        def generate(self, _inputs, **options):
            captured["generated"] = options
            return CanvasCompletion(complete=True)

    monkeypatch.setenv("ARK_API_KEY", "fake-key")
    monkeypatch.setattr("server.service.video_parse.ArkMultimodalModel", Model)
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = load_config()
    binding = GenerativeModelConfig(source="ark_default", model="model", request_timeout_seconds=60,
                                    max_output_tokens=max_output_tokens, reasoning_effort="none")
    result = runtime._invoke_multimodal(binding, [], schema_name="CanvasCompletion", result_type=CanvasCompletion,
                                       tools=(object(),), max_model_calls=3)
    assert result.complete
    assert captured["constructed"]["max_output_tokens"] is max_output_tokens
    assert captured["constructed"]["reasoning_effort"] == "none"
    policy = captured["generated"]["context_window_policy"]
    if max_output_tokens == 131_072:
        assert policy is None
    else:
        assert policy.auto_compact_tokens < policy.maximum_input_tokens
        assert policy.compact_target_tokens < policy.auto_compact_tokens
    assert captured["generated"]["max_input_chars"] == 400_000


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
    runtime._tracing = None
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


def test_model_stage_traces_actual_worker_calls_without_a_ledger(monkeypatch):
    from threading import get_ident

    main_thread = get_ident()
    traced, thread_ids, outer = [], [], []

    def observe(call):
        traced.append(call)
        thread_ids.append(get_ident())

    class Tracing:
        @contextmanager
        def scope(self, context):
            with model_call_context(context), capture_model_usage(observe, combine=True):
                yield

    class Manager:
        def __init__(self, *_args, **_kwargs):
            pass

        def capture(self):
            return nullcontext()

        def cancel(self):
            pass

    monkeypatch.setattr("server.service.video_parse.VideoParseModelCallManager", Manager)
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._repository = object()
    runtime._queue = object()
    runtime._lease_duration = timedelta(seconds=5)
    runtime._heartbeat_interval = timedelta(seconds=1)
    runtime._parse_usage = None
    runtime._tracing = Tracing()
    runtime._model_manager = ContextVar("tracing_test_manager", default=None)
    lease = VideoParseLease(
        video_parse_task_id="task", video_id="video", worker_id="worker",
        lease_version=9, lease_expire_time=datetime.now(UTC) + timedelta(seconds=5),
        attempt_count=2, max_attempts=3, parse_config={},
    )
    task_input = SimpleNamespace(
        tenant_id="workspace", series_id="series",
        task=SimpleNamespace(video_id="video"),
    )

    def invoke():
        with track_model_call(provider="fixture", model="chat", operation="video_parse"):
            pass

    async def execute():
        with capture_model_usage(outer.append):
            async with runtime._model_stage(
                lease, task_input, sequence_no=2, category="parse",
                check_active=lambda: None,
            ):
                await runtime._run_blocking(invoke)

    asyncio.run(execute())
    assert len(traced) == len(outer) == 1
    assert thread_ids[0] != main_thread
    assert traced[0].context["tenant_id"] == "workspace"
    assert traced[0].context["video_parse_task_id"] == "task"
    assert traced[0].context["lease_version"] == 9
    assert traced[0].context["attempt_count"] == 2
    assert traced[0].context["sequence_no"] == 2


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
