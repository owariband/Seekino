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
from agent.video_parse_v2.prompts import build_canvas_understanding_prompt
from agent.video_parse_v2.state import ChunkExecution, LeaseToken
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseContextCanvas
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
from gateway.config import (
    ArkModelSourceConfig, CodexModelSourceConfig, ModelBinding,
    OpenAIAudioSourceConfig, load_config,
)
from gateway.ark.embedding import DOUBAO_MULTIMODAL_EMBEDDING_REVISION
from gateway.embedding import EmbeddingResult
from gateway.errors import ContentFilterError
from gateway.multimodal import ImageInput, TextInput, VideoInput
from gateway.model_factory import GatewayClient
from gateway.usage import TokenUsage, capture_model_usage, model_call_context, track_model_call

from server.api.app import create_app
from server.bootstrap import ApplicationServices
from engine.common.config import VideoParseRuntimeConfig
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig, default_video_parse_v2_config, index_contract_fingerprint
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
from server.service.video_parse_persistence import RUNTIME_DETAIL_KEY, VideoParsePersistenceService
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


def _read_context_tool():
    from langchain_core.tools import StructuredTool

    def read_context() -> str:
        return "{}"

    return StructuredTool.from_function(read_context, description="Read the current parse context.")


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
            self.is_current(
                {name: getattr(document, name) for name in (
                    "tenant_id", "series_id", "video_id", "parse_task_id",
                    "video_chunk_id", "content_version", "index_generation",
                    "chunk_start_seconds", "chunk_end_seconds",
                )},
                semantic_required=document.semantic_vector is not None,
                sparse_required=document.sparse_token_weights is not None,
            )
            for document in documents
        )

    def is_current(self, expected, *, semantic_required, sparse_required=False) -> bool:
        document = self.documents.get(document_id(
            expected["parse_task_id"], expected["video_chunk_id"],
        ))
        return (
            document is not None
            and all(getattr(document, name) == value for name, value in expected.items())
            and len(document.vision_vector) == self.vision_dimensions
            and (not semantic_required or len(document.semantic_vector or ()) == self.semantic_dimensions)
            and (not sparse_required or document.sparse_token_weights is not None)
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
        if self.fail_next_bulk and len(self.bulk_keys) == 2:
            self.fail_next_bulk = False
            raise RuntimeError("injected Chunk indexing failure")
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
    media = PreparedVideoParseMedia(video, audio, (), visual_video_path=tmp_path / "context.mp4",
                                    visual_start_ms=9000, visual_end_ms=18_000)
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
            {"start": 1.5, "end": 4, "text": "hello", "speaker_id": "speaker1"},
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
                {"start_seconds": 1.5, "end_seconds": 4, "text": "hello", "speaker_id": "speaker1"},
                {"start_seconds": 3, "end_seconds": 4, "text": "outside", "speaker_id": None},
            ]})

        return SimpleNamespace(model_name=model, generate=generate)

    monkeypatch.setattr(runtime, "_ensure_chunk_media", MethodType(prepared_media, runtime))
    monkeypatch.setattr("gateway.transcription.transcribe_audio", transcribe)
    monkeypatch.setattr("gateway.transcription.ArkMultimodalModel", build_model)
    result = asyncio.run(runtime.transcribe_chunk(execution))

    assert result == {"transcript": [] if adapter == "none" else [{
        "start_seconds": 13_500, "end_seconds": 15_000, "text": "hello", "speaker_id": "speaker1",
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
            "reasoning_effort": None, "api_key": "test-key",
        })]
    else:
        assert calls == []


@pytest.mark.parametrize("start_ms,end_ms,overlap_seconds,visual_bounds", [
    (0, 60_000, 3, (0, 63_000)),
    (60_000, 120_000, 3, (57_000, 121_000)),
    (120_000, 121_000, 3, (117_000, 121_000)),
    (60_000, 120_000, 0, (60_000, 120_000)),
])
def test_visual_context_is_clamped_without_expanding_core_media(
    monkeypatch, tmp_path, start_ms, end_ms, overlap_seconds, visual_bounds,
):
    config = _parse_config()
    config["visual_input"]["context_overlap_seconds"] = overlap_seconds
    inputs = ParseInput.model_validate({"series_id": "series", "video_id": "video", "parse_config": config})
    execution = ChunkExecution(
        str(uuid4()), str(uuid4()), 1, start_ms, end_ms, start_ms, end_ms, inputs,
        LeaseToken("worker", 1, datetime.now(UTC) + timedelta(seconds=30)),
    )
    binding = SimpleNamespace(
        lock=asyncio.Lock(), media=None, media_context=None, lease=object(),
        state=SimpleNamespace(source_media_ref=str(tmp_path / "source.mp4"),
                              task_input=SimpleNamespace(duration_seconds=121_000)),
    )
    calls = []

    @contextmanager
    def prepare(_identity, **options):
        calls.append(options)
        yield PreparedVideoParseMedia(tmp_path / "core.mp4", tmp_path / "core.wav", ())

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._artifact_store = _ArtifactResolver()
    runtime._media_service = SimpleNamespace(prepare_chunk=prepare)
    monkeypatch.setattr(runtime, "_require_chunk_execution", lambda _execution: binding)
    monkeypatch.setattr(runtime, "_require_chunk_active", lambda _lease: None)

    async def run():
        media = await runtime._ensure_chunk_media(execution)
        assert await runtime._ensure_chunk_media(execution) is media
        await runtime.cleanup_chunk_execution(execution)

    asyncio.run(run())
    assert len(calls) == 1
    assert (calls[0]["start_seconds"], calls[0]["end_seconds"]) == (start_ms, end_ms)
    assert (calls[0]["visual_start_ms"], calls[0]["visual_end_ms"]) == visual_bounds
    assert binding.media is None and binding.media_context is None


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
        ((1000, tmp_path / "frame-1.jpg"), (21_000, tmp_path / "context.jpg")),
        ocr_frames=((1000, tmp_path / "frame-1.jpg"), (2000, tmp_path / "frame-2.jpg")),
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
    config["indexing"] = {
        "index_generation": 1,
        "sparse_enabled": True,
        "contract_fingerprint": index_contract_fingerprint(
            inputs.parse_config.models.embedding.model_copy(update={"encoder_model": expected_model}),
            provider="ark",
            encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
            max_height=inputs.parse_config.visual_input.max_height,
        ),
    }
    inputs = ParseInput.model_validate({"series_id": "series", "video_id": "video", "parse_config": config})
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
    monkeypatch.setattr("arkruntime.Ark.volc", lambda **_kwargs: client)
    runtime = VideoParseRuntimeService(
        object(), object(), _ArtifactResolver(),
        VideoParseMediaService(tmp_path / "media"),
        load_config(), _ElasticsearchBoundary(),
        worker_id="endpoint-worker", lease_duration=timedelta(seconds=30),
        heartbeat_interval=timedelta(seconds=5),
    )
    coordinator, selected_client = runtime._build_index_coordinator(inputs, check_active=lambda: None, index_generation=1)
    try:
        assert selected_client is coordinator._embedder
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


def test_index_builder_closes_gateway_model_when_store_dimensions_differ(tmp_path):
    config = _parse_config()
    config["models"]["embedding"]["dimensions"] = 3
    inputs = ParseInput.model_validate({
        "series_id": "series", "video_id": "video", "parse_config": config,
    })
    config["indexing"]["contract_fingerprint"] = index_contract_fingerprint(
        inputs.parse_config.models.embedding, provider="ark", encoder_revision="test-revision",
        max_height=inputs.parse_config.visual_input.max_height,
    )
    inputs = ParseInput.model_validate({"series_id": "series", "video_id": "video", "parse_config": config})
    closed = []
    embedder = SimpleNamespace(
        provider_name="ark", dimensions=3,
        encoder_model="doubao-embedding-vision-250615", encoder_revision="test-revision",
        close=lambda: closed.append(True),
    )
    runtime = VideoParseRuntimeService(
        object(), object(), _ArtifactResolver(),
        VideoParseMediaService(tmp_path / "media"), load_config(), _ElasticsearchBoundary(),
        worker_id="worker", lease_duration=timedelta(seconds=30),
        heartbeat_interval=timedelta(seconds=5),
        gateway_client=SimpleNamespace(create_embedding_model=lambda _binding: embedder),
    )
    with pytest.raises(ValueError, match="semantic dimensions differ"):
        runtime._build_index_coordinator(inputs, check_active=lambda: None, index_generation=1)
    assert closed == [True]


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
    repository = VideoParsePersistenceService(factory)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
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

    def build_index_coordinator(_self, inputs, *, check_active, index_generation):
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
        poll_interval=timedelta(milliseconds=10),
        maintenance_interval=timedelta(seconds=5),
        video_concurrency=3,
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
    repository = VideoParsePersistenceService(factory)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    store = _RecoveringElasticsearchBoundary()
    media = VideoParseMediaService(
        tmp_path / "media", command_runner=_write_media,
        probe_duration=lambda _path: 10_000,
    )
    parse_config = _parse_config()
    parse_config["interval_duration"] = interval_duration_seconds
    parse_config["execution"]["max_retry_count"] = 0
    # This component fixture uses SQLite; MySQL tests own parallel lock evidence.
    parse_config["execution"].update(parse_concurrency=1, refine_concurrency=1, index_concurrency=1)
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
        parse_defaults=ParseConfig.from_defaults(
            runtime=VideoParseRuntimeConfig(), gateway=load_config(),
            upload=UploadServiceConfig(), images=ImageUploadConfig(),
            embedding_model=None, embedding_dimensions=2, embedding_fps=None,
        ),
        video_parse_index_store=store,
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

    def build_index_coordinator(_self, inputs, *, check_active, index_generation):
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
        # SQLite cannot enforce FOR UPDATE; true lock competition is a MySQL case.
        maintenance_interval=timedelta(seconds=10),
        video_concurrency=3,
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
            assert len(store.bulk_keys) == 2
            assert all(len(keys) == 1 for keys in store.bulk_keys)
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
                "/api/uploads/retry-parse", params={
                    **scope, "expectedJobVersion": 1,
                    "expectedLeaseVersion": failed.json()["leaseVersion"],
                },
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
            assert len(store.bulk_keys) == 3
            assert store.bulk_keys[2] == store.bulk_keys[1]
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


@pytest.mark.parametrize("source,visual_mode", [
    ("ark_default", "video"),
    ("ark_default", "ordered_frames"),
    ("deepseek_api", "ordered_frames"),
])
@pytest.mark.parametrize("explicit_refine", [False, True])
@pytest.mark.parametrize("overlap_ms", [0, 3000])
def test_understanding_and_refine_share_deepagents_with_bound_media_and_canvas(
    monkeypatch, tmp_path, source, visual_mode, explicit_refine, overlap_ms,
):
    requests = []
    closed = []

    class Model:
        def __init__(self, model, **_options):
            self.name = model
            self.options = _options

        def generate(self, *_args, **_options):
            pytest.fail("understanding/refine must use the shared DeepAgents harness")

        def close(self):
            closed.append(self.name)

    def generate_with_deepagents(adapter, request_inputs, **options):
        if source == "deepseek_api":
            assert adapter.options["base_url"] == "https://api.deepseek.com"
            assert adapter.options["max_output_tokens"] == 8192
            assert adapter.options["reasoning_effort"] == "none"
        requests.append((adapter.name, list(request_inputs), options))
        tools = {tool.name: tool for tool in options["tools"]}
        assert set(tools) == {"get_context_canvas", "write_context_canvas"} | (
            {"inspect_video_frames"} if len(requests) > 1 else set()
        )
        evidence = json.loads(tools["get_context_canvas"].invoke({
            "collection": "evidence", "reference": "member", "section": "asr",
        }))
        assert "transcript" in evidence["content"]
        if len(requests) == 1:
            if overlap_ms:
                rejected = json.loads(tools["write_context_canvas"].invoke({
                    "batch_id": "outside-context", "expected_revision": evidence["revision"],
                    "operations": [{
                        "op": "add_event", "member_ref": "member", "key": "context-only",
                        "event": ActionEvent(
                            event_type="ACTION", start_seconds=19_000, end_seconds=19_500,
                            description="Outside context must not be written.", participants=[], visible_persons=[],
                        ).model_dump(mode="json"),
                    }],
                }))
                assert "error" in rejected
            receipt = json.loads(tools["write_context_canvas"].invoke({
                "batch_id": "draft", "expected_revision": evidence["revision"],
                "operations": [{
                    "op": "add_event", "member_ref": "member", "key": "action",
                    "event": ActionEvent(
                        event_type="ACTION", start_seconds=21_000, end_seconds=23_000,
                        description="A supported test action.", participants=[], visible_persons=[],
                    ).model_dump(mode="json"),
                }],
            }))
            assert receipt["revision"] > evidence["revision"]
        assert options["result_type"] is CanvasCompletion
        assert options["max_model_calls"] is None
        assert options["context_checkpoint"] is not None
        return CanvasCompletion(complete=True)

    monkeypatch.setenv("ARK_API_KEY", "fake-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-deepseek-key")
    monkeypatch.setattr("gateway.ark.multimodal.ArkMultimodalModel", Model)
    monkeypatch.setattr("gateway.openai.multimodal.DeepSeekMultimodalModel", Model)
    monkeypatch.setattr("gateway.agent_model.deepagents_multimodal.generate_with_deepagents", generate_with_deepagents)
    config = _parse_config()
    config["visual_input"]["mode"] = visual_mode
    config["models"]["understanding"] = {
        "source": source, "model": "understanding-model", "request_timeout_seconds": 61,
        "fallback": {
            "source": "deepseek_api" if source == "ark_default" else "ark_default",
            "model": "fallback-model", "request_timeout_seconds": 17,
            "max_output_tokens": 8192, "reasoning_effort": "none",
        },
    }
    config["models"]["refine"] = ({
        "source": source, "model": "refine-model", "request_timeout_seconds": 17,
    } if explicit_refine else None)
    inputs = ParseInput.model_validate({
        "series_id": "series", "video_id": "video", "parse_config": config,
    })
    execution = ChunkExecution(
        "member", "video-task", 2, 20_000, 30_000, 20_000, 30_000, inputs,
        LeaseToken("worker", 1, datetime.now(UTC) + timedelta(seconds=30)),
    )
    media = PreparedVideoParseMedia(
        tmp_path / "prepared.mp4", tmp_path / "prepared.wav",
        ((21_000, tmp_path / "frame-1.jpg"), (22_000, tmp_path / "frame-2.jpg")),
        visual_video_path=tmp_path / "context.mp4" if overlap_ms else None,
        visual_start_ms=20_000 - overlap_ms,
        visual_end_ms=30_000 + overlap_ms,
    )
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = load_config()
    runtime._gateway_config.normalized_models["vision"] = (
        runtime._gateway_config.normalized_models["vision"].model_copy(
            update={"source": "ark_default", "model": "old-pro-model"}
        )
    )
    runtime._gateway = GatewayClient(runtime._gateway_config)
    runtime._model_manager = ContextVar("shared-harness-manager", default=None)

    async def prepared_media(_execution):
        assert _execution is execution
        return media

    monkeypatch.setattr(runtime, "_ensure_chunk_media", prepared_media)
    evidence = {"member": {"asr": {"transcript": []}, "ocr": None}}
    canvas = ParseContextCanvas(scope="chunk:member", stage="understanding", evidence=evidence, members={
        "member": {"sequence_no": 2, "version": 0, "bounds": (20_000, 30_000),
                   "content": VideoParseResult(persons=[], timeline=[])},
    })
    result = asyncio.run(runtime.understand_chunk(
        execution, asr_result={"transcript": []}, ocr_result=None,
        prompt=build_canvas_understanding_prompt(execution, canvas=canvas), canvas=canvas,
    ))
    assert len(result.timeline) == 1
    refine_canvas = ParseContextCanvas(scope="refine:interval", evidence=evidence, members={
        "member": {"sequence_no": 2, "version": 1, "bounds": (20_000, 30_000), "content": result},
    })
    runtime._persistence = SimpleNamespace(load_task_input=lambda _task: SimpleNamespace(
        series_id="series", task=SimpleNamespace(video_id="video"), duration_seconds=30_000,
        original_source_uri=str(tmp_path / "source.mp4"), content_hash="source-hash",
    ))
    runtime._artifact_store = _ArtifactResolver()
    monkeypatch.setattr(runtime, "_require_interval_token", lambda *_args, **_kwargs: object())
    assert asyncio.run(runtime.generate_interval_refinement(
        {"inputs": inputs, "video_parse_task_id": "video-task", "interval_parse_task_id": "interval",
         "lease": execution.lease, "member_bounds": {"member": (20_000, 30_000)}},
        prompt="refine this interval", canvas=refine_canvas,
    )).complete
    assert [request[0] for request in requests] == [
        "understanding-model", "refine-model" if explicit_refine else "understanding-model",
    ]
    primary_inputs = requests[0][1]
    assert f"Host visual input range: [{20_000 - overlap_ms}, {30_000 + overlap_ms}) ms" in primary_inputs[-1].text
    assert "Writable chunk range: [20000, 30000) ms" in primary_inputs[-1].text
    assert f"absolute_ms = {20_000 - overlap_ms} + round(local_seconds * 1000)" in primary_inputs[-1].text
    assert "Prefer leaving a low-confidence or conflicting attribution unknown" in primary_inputs[-1].text
    assert ("We supplied additional visual context" in primary_inputs[-1].text) is bool(overlap_ms)
    if visual_mode == "video":
        assert primary_inputs[0] == VideoInput(media.visual_video_path or media.video_path,
                                              inputs.parse_config.visual_input.fps)
    else:
        assert primary_inputs[:-1] == [ImageInput(path=path) for _timestamp, path in media.frames]
        assert primary_inputs[-1].text.endswith("Ordered frame timestamps: [21000, 22000]")
    fallback_inputs = requests[0][2]["fallback_inputs"]
    assert fallback_inputs[:-1] == [ImageInput(path=path) for _timestamp, path in media.frames]
    assert fallback_inputs[-1].text.endswith("Fallback ordered frame timestamps: [21000, 22000]")
    assert requests[1][1] == [TextInput("refine this interval")]
    assert requests[1][2]["fallback_inputs"] is None
    assert closed == ["fallback-model", "understanding-model"] + (
        ["refine-model"] if explicit_refine else ["fallback-model", "understanding-model"]
    )
    runtime._gateway.close()


@pytest.mark.parametrize("tool_harness", ["legacy", "deepagents"])
def test_canvas_tool_channel_receives_frozen_fallback_controls(monkeypatch, tool_harness):
    captured = {}
    closed = []

    class Primary:
        def __init__(self, model, **options):
            captured["primary"] = (model, options)

        def generate(self, inputs, **options):
            captured.setdefault("harness", "legacy")
            captured["generate"] = options
            return CanvasCompletion(complete=True)

        def close(self):
            closed.append("primary")

    class Fallback:
        def __init__(self, model, **options):
            captured["fallback"] = (model, options)

        def close(self):
            closed.append("fallback")

    monkeypatch.setattr("gateway.openai.multimodal.DeepSeekMultimodalModel", Primary)
    monkeypatch.setattr("gateway.ark.multimodal.ArkMultimodalModel", Fallback)

    def generate_with_deepagents(adapter, request_inputs, **options):
        assert isinstance(adapter, Primary)
        captured["harness"] = "deepagents"
        return adapter.generate(request_inputs, **options)

    monkeypatch.setattr("gateway.agent_model.deepagents_multimodal.generate_with_deepagents", generate_with_deepagents)
    monkeypatch.setenv("ARK_API_KEY", "fake-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key")
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = load_config()
    runtime._gateway = GatewayClient(runtime._gateway_config)
    runtime._model_manager = ContextVar("fallback-controls-manager", default=None)
    binding = GenerativeModelConfig(
        source="deepseek_api", model="primary-model", request_timeout_seconds=61,
        max_output_tokens=4096, reasoning_effort="none",
        fallback={"source": "ark_default", "model": "fallback-model", "request_timeout_seconds": 17,
                  "max_output_tokens": 2048, "reasoning_effort": "none"},
    )
    result = asyncio.run(runtime._generate_text_structured(
        binding, "refine", tools=(_read_context_tool(),), result_type=CanvasCompletion, max_model_calls=100,
        tool_harness=tool_harness,
    ))
    assert result.complete
    assert captured["harness"] == tool_harness
    assert captured["primary"][1]["request_timeout_seconds"] == 61
    assert captured["fallback"][1]["request_timeout_seconds"] == 17
    assert captured["fallback"][1]["max_output_tokens"] == 2048
    assert captured["fallback"][1]["reasoning_effort"] == "none"
    assert captured["generate"]["max_model_calls"] is None
    assert captured["generate"]["request_timeout_seconds"] == 61
    assert captured["generate"]["context_window_policy"].output_reserve_tokens == 4096
    assert captured["generate"]["fallback_on_error"](ContentFilterError("filtered"))
    assert not captured["generate"]["fallback_on_error"](ValueError("invalid response"))
    assert closed == ["fallback", "primary"]
    assert not runtime._gateway._resources
    runtime._gateway.close()


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
    monkeypatch.setattr("gateway.ark.multimodal.ArkMultimodalModel", _Model)
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = load_config()
    runtime._gateway = GatewayClient(runtime._gateway_config)
    binding = GenerativeModelConfig(
        source="ark_default", model="frozen-model", request_timeout_seconds=61,
        max_output_tokens=131072, reasoning_effort=reasoning_effort,
        context_window_tokens=262144,
    )

    result = runtime._invoke_multimodal(
        binding, [TextInput("refine")], schema_name="refine", result_type=IntervalRefinementProposal,
    )

    assert result.updates == []
    assert captured["max_output_tokens"] == 131072
    assert captured["reasoning_effort"] == reasoning_effort
    assert captured["request_timeout_seconds"] == 61
    runtime._gateway.close()


def test_frozen_codex_visual_model_uses_gateway_native_harness(monkeypatch, tmp_path):
    from gateway.codex.agent import CodexAgentModel
    from openai_codex import ImageInput as CodexImageInput, TextInput as CodexTextInput

    constructed = []
    sessions = []

    class Session:
        tool_call_count = successful_tool_call_count = 0

        def __init__(self, **options):
            self.options = options
            self.closed = False
            self.inputs = None

        def run_turn(self, inputs, *, output_schema):
            self.inputs = inputs
            assert output_schema["type"] == "object"
            assert self.options["tools"][0].invoke({}) == "{}"
            self.tool_call_count = self.successful_tool_call_count = 1
            return SimpleNamespace(final_response='{"updates": []}', usage=None)

        def close(self):
            self.closed = True

        interrupt = close

    def create_session(**options):
        session = Session(**options)
        sessions.append(session)
        return session

    def native_model(**options):
        constructed.append(options)
        return CodexAgentModel(**options, session_factory=create_session)

    def unexpected_api_harness(*_args, **_options):
        pytest.fail("Codex visual input must use the Gateway native harness")

    monkeypatch.setattr("gateway.codex.agent.CodexAgentModel", native_model)
    monkeypatch.setattr("gateway.agent_model.deepagents_multimodal.generate_with_deepagents", unexpected_api_harness)
    monkeypatch.setattr("gateway.agent_model.multimodal.generate_with_tools", unexpected_api_harness)
    monkeypatch.setenv("ANIVAG_VIDEO_MODEL", "outdated-remote-endpoint")
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = load_config()
    runtime._gateway_config.model_sources["native_codex"] = CodexModelSourceConfig()
    runtime._gateway = GatewayClient(runtime._gateway_config)
    real_create_model = runtime._gateway.create_generative_model

    def create_model(definition, **options):
        assert options["mcp"] is False
        return real_create_model(definition, **options)

    monkeypatch.setattr(runtime._gateway, "create_generative_model", create_model)
    binding = GenerativeModelConfig(
        source="native_codex", model="frozen-vision-gpt", request_timeout_seconds=10,
        max_output_tokens=2048, reasoning_effort="high", supports_vision_input=True,
    )
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first frame")
    second.write_bytes(b"second frame")
    result = runtime._invoke_multimodal(
        binding, [ImageInput(first), TextInput("frame sequence"), ImageInput(second)],
        schema_name="refinement", result_type=IntervalRefinementProposal,
        tools=(_read_context_tool(),), tool_harness="deepagents",
    )
    assert result.updates == []
    assert constructed[0]["model"] == "frozen-vision-gpt"
    assert constructed[0]["max_output_tokens"] == 2048
    assert constructed[0]["reasoning_effort"] == "high"
    assert constructed[0]["mcp"] is False
    assert [type(item) for item in sessions[0].inputs] == [CodexImageInput, CodexTextInput, CodexImageInput]
    assert sessions[0].inputs[0].url != sessions[0].inputs[2].url
    assert all(item.url.startswith("data:image/png;base64,") for item in (sessions[0].inputs[0], sessions[0].inputs[2]))
    assert sessions[0].successful_tool_call_count == 1
    assert sessions[0].closed
    assert not runtime._gateway._resources
    runtime._gateway.close()


@pytest.fixture
def voiceprint_host(monkeypatch, tmp_path):
    import wave

    from agent.utils.context_canvas import ContextCanvas
    from gateway.speaker_embedding import SpeakerEmbeddingResult
    from server.utils.execution import ExecutionCalls, execution_calls
    from server.service.video_parse_model_calls import VideoParseModelCallManager

    values = default_video_parse_v2_config().model_dump(mode="json")
    values["voiceprint"] = {"max_tool_calls": 12, "execution_timeout_seconds": 2.0}
    values["models"]["voiceprint"] = {
        "source": "ark_default", "model": "voice-selector", "request_timeout_seconds": 2.0,
        "supports_vision_input": False,
    }
    values["models"]["speaker_embedding"] = {
        "source": "speaker_cpu", "model": "campplus", "request_timeout_seconds": 2.0,
    }
    values["models"]["understanding"]["supports_vision_input"] = False
    inputs = ParseInput.model_validate({"series_id": "series", "video_id": "video", "parse_config": values})
    lease = VideoParseLease(str(uuid4()), "video", "worker", 2, datetime.now(UTC), 1, 3, values)
    original = tmp_path / "original.mp4"
    original.write_bytes(b"original video")
    state = SimpleNamespace(
        commands=[], audio_paths=[], registrations=[], unregistrations=[], notifications=[], stages=[], checks=[],
        closed=False, ready=False, coverage=False, calls=ExecutionCalls(),
    )

    def write_audio(command):
        state.commands.append(command)
        path = Path(command[-1])
        state.audio_paths.append(path)
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x01\x00" * 27200)

    class Encoder:
        model_name = "iic/speech_campplus_sv_zh-cn_16k-common"
        encoder_revision = "v1.0.0"
        dimensions = 192

        def embed_audio(self, path):
            assert Path(path).is_file()
            return SpeakerEmbeddingResult(
                model=self.model_name, encoder_revision=self.encoder_revision,
                vector=[1.0] + [0.0] * 191, duration_ms=1700,
            )

        def close(self):
            state.closed = True

    encoder = Encoder()
    source = SimpleNamespace(
        duration_seconds=60_000, original_source_uri="artifact://original", series_id="series",
        task=SimpleNamespace(video_id="video", detail={"voiceprint": {"ready": False}}),
    )

    def register(_lease, **arguments):
        assert _lease is lease
        state.registrations.append(arguments)
        return {"registered": True, "reference": arguments}

    def finish(_lease, **arguments):
        assert _lease is lease
        state.ready = state.coverage
        source.task.detail["voiceprint"]["ready"] = state.ready
        return {"ready": state.ready, "missing_speakers": [] if state.ready else ["燈"],
                "registered_speaker_count": 2 if state.ready else 1, "minimum_speaker_count": 2}

    def unregister(_lease, *, source_ref):
        assert _lease is lease
        state.unregistrations.append(source_ref)
        return {"removed": True, "speaker": "爽世", "source_ref": source_ref,
                "refs_count": 1, "target_refs_count": 5}

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = SimpleNamespace(
        load_task_input=lambda _task: source,
        precheck_voiceprint_registration=lambda _lease, **_values: None,
        register_voiceprint=register, finish_voiceprint_preparation=finish,
        unregister_voiceprint=unregister,
        get_voiceprint_speaker_list=lambda _lease: {"speaker_list": [{
            "speaker": "爽世", "chunk_ids": ["chunk-0"], "refs_count": 0,
            "target_refs_count": 5, "references": [],
        }]},
    )
    runtime._artifact_store = SimpleNamespace(resolve=lambda uri: original if uri == "artifact://original" else pytest.fail(uri))
    runtime._media_service = VideoParseMediaService(tmp_path / "media", command_runner=write_audio)
    runtime._model_manager = ContextVar("voiceprint-host-manager", default=None)
    runtime._video_lease = ContextVar("voiceprint-host-lease", default=lease)
    monkeypatch.setattr(runtime, "_require_video_active", lambda _lease: None)
    monkeypatch.setattr(runtime, "_speaker_encoder", lambda _binding: encoder)
    monkeypatch.setattr(runtime, "_notify_change", state.notifications.append)

    @asynccontextmanager
    async def model_stage(_lease, _source, **options):
        state.stages.append(options)
        calls_token = execution_calls.set(state.calls)
        manager = VideoParseModelCallManager(
            runtime._persistence, execution_lease=lease, lease_duration=timedelta(minutes=2),
            check_active=lambda: state.checks.append("active"),
        )
        state.manager = manager
        manager_token = runtime._model_manager.set(manager)
        try:
            yield manager
        finally:
            runtime._model_manager.reset(manager_token)
            execution_calls.reset(calls_token)

    monkeypatch.setattr(runtime, "_model_stage", model_stage)
    snapshot = {
        "video_parse_task_id": lease.video_parse_task_id, "inputs": inputs,
        "sources": {"source-0": {"speaker": "爽世", "start_ms": 47_000, "end_ms": 50_000}},
    }
    canvas = ContextCanvas(scope="voiceprint:host")
    canvas.mutate(lambda draft: draft.update({"sources": snapshot["sources"]}), readonly_collections=("sources",))
    return SimpleNamespace(runtime=runtime, snapshot=snapshot, canvas=canvas, lease=lease,
                           encoder=encoder, source=source, original=original, state=state)


def test_voiceprint_host_tools_bind_original_audio_and_persist_before_notification(monkeypatch, voiceprint_host):
    host = voiceprint_host
    observed = {}

    async def generate(_binding, _prompt, *, tools, **options):
        observed.update(options)
        tools = {tool.name: tool for tool in tools}
        assert set(tools) == {"get_context_canvas", "get_speaker_list", "register_voiceprint",
                              "unregister_voiceprint", "finish_voiceprint_preparation"}
        assert json.loads(tools["get_speaker_list"].invoke({}))["speaker_list"][0]["speaker"] == "爽世"
        assert "source-0" in tools["get_context_canvas"].invoke({"collection": "sources"})
        reference = json.loads(tools["register_voiceprint"].invoke({
            "source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100, "end_ms": 48_800,
            "selection_basis": "已提交的独立台词，画面人物与前后声音一致", "confidence": 0.9,
        }))
        assert reference["dimension"] == 192 and "vector" not in reference["reference"]
        assert not any(path.exists() for path in host.state.audio_paths)
        assert json.loads(tools["finish_voiceprint_preparation"].invoke({"complete": True}))["ready"] is False
        assert host.state.notifications == []
        host.state.coverage = True
        assert json.loads(tools["finish_voiceprint_preparation"].invoke({"complete": True}))["ready"] is True
        assert host.source.task.detail["voiceprint"]["ready"] is True
        assert host.state.notifications == [host.lease.video_parse_task_id]
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    result = asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas))
    assert result.complete and host.state.closed
    assert observed["enforce_model_call_limit"] is True
    assert observed["max_model_calls"] == host.snapshot["inputs"].parse_config.execution.max_model_calls
    assert host.state.stages[0]["stage"] == "voiceprint"
    command = host.state.commands[0]
    assert command[command.index("-i") + 1] == str(host.original)
    assert command[command.index("-ss") + 1] == "47.100"
    assert command[command.index("-t") + 1] == "1.700"
    assert command[command.index("-ar") + 1] == "16000"
    registered = host.state.registrations[0]
    assert (registered["start_ms"], registered["end_ms"]) == (47_100, 48_800)
    assert len(registered["vector"]) == 192 and len(registered["pcm_sha256"]) == 64
    asyncio.run(host.runtime.ensure_voiceprint_prepared(host.snapshot))


def test_voiceprint_model_completion_cannot_replace_finish_commit(monkeypatch, voiceprint_host):
    from unittest.mock import AsyncMock

    host = voiceprint_host
    monkeypatch.setattr(host.runtime, "_generate_text_structured", AsyncMock(return_value=CanvasCompletion(complete=True)))
    assert asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="done", canvas=host.canvas)).complete
    with pytest.raises(ValueError, match="successful finish Tool commit"):
        asyncio.run(host.runtime.ensure_voiceprint_prepared(host.snapshot))
    assert not host.state.ready and host.state.notifications == []


@pytest.mark.parametrize("first_tool", ["get_context_canvas", "unregister_voiceprint"])
def test_voiceprint_tool_budget_counts_reads_and_closes_encoder_on_exhaustion(monkeypatch, voiceprint_host, first_tool):
    from server.service.video_parse_persistence import ModelCallBudgetExhaustedError

    host = voiceprint_host
    config = host.snapshot["inputs"].parse_config
    host.snapshot["inputs"] = host.snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(update={
        "voiceprint": config.voiceprint.model_copy(update={"max_tool_calls": 1}),
    })})

    async def generate(_binding, _prompt, *, tools, **_options):
        tools = {tool.name: tool for tool in tools}
        tools[first_tool].invoke({"collection": "sources"} if first_tool == "get_context_canvas"
                                 else {"source_ref": "source-old"})
        tools["get_speaker_list"].invoke({})
        pytest.fail("A second Tool call exceeded the frozen preparation budget")

    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    with pytest.raises(ModelCallBudgetExhaustedError, match="max_tool_calls"):
        asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas))
    assert host.state.closed and not host.state.ready


@pytest.mark.parametrize("failure_at", ["encoder", "audio", "precheck", "register", "unregister", "speaker_list", "finish"])
@pytest.mark.parametrize("generation_outcome", ["complete", "another_error"])
def test_voiceprint_host_failure_survives_tool_feedback_and_fences_finish(
    monkeypatch, voiceprint_host, failure_at, generation_outcome,
):
    host = voiceprint_host
    original = TimeoutError("cold encoder timed out") if failure_at == "encoder" else RuntimeError(failure_at)

    def fail(*_args, **_kwargs):
        raise original

    if failure_at == "encoder":
        monkeypatch.setattr(host.encoder, "embed_audio", fail)
    elif failure_at == "audio":
        monkeypatch.setattr(host.runtime, "_embed_speaker_segment", fail)
    else:
        method = {"precheck": "precheck_voiceprint_registration", "register": "register_voiceprint",
                  "unregister": "unregister_voiceprint",
                  "speaker_list": "get_voiceprint_speaker_list",
                  "finish": "finish_voiceprint_preparation"}[failure_at]
        monkeypatch.setattr(host.runtime._persistence, method, fail)

    async def generate(_binding, _prompt, *, tools, **_options):
        tools = {tool.name: tool for tool in tools}
        name = "get_speaker_list" if failure_at == "speaker_list" else (
            "finish_voiceprint_preparation" if failure_at == "finish" else (
                "unregister_voiceprint" if failure_at == "unregister" else "register_voiceprint"))
        values = {} if name == "get_speaker_list" else {"complete": True} if name == "finish_voiceprint_preparation" else {
            "source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100, "end_ms": 48_800,
            "selection_basis": "evidence",
        }
        if name == "unregister_voiceprint":
            values = {"source_ref": "source-old"}
        # Codex turns exceptions into tool feedback; the host must retain its cause.
        with pytest.raises(type(original)) as caught:
            tools[name].invoke(values)
        assert caught.value is original
        host.state.coverage = True
        with pytest.raises(type(original)) as caught:
            tools["finish_voiceprint_preparation"].invoke({"complete": True})
        assert caught.value is original
        if generation_outcome == "another_error":
            raise RuntimeError("provider stopped after cancelled Tool")
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    with pytest.raises(type(original)) as caught:
        asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas))
    assert caught.value is original
    assert not host.state.ready and host.state.notifications == [] and host.state.closed
    assert all(not path.exists() for path in host.state.audio_paths)


def test_voiceprint_swallowed_tool_budget_failure_still_fails_preparation(monkeypatch, voiceprint_host):
    from server.service.video_parse_persistence import ModelCallBudgetExhaustedError

    host = voiceprint_host
    config = host.snapshot["inputs"].parse_config
    host.snapshot["inputs"] = host.snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(update={
        "voiceprint": config.voiceprint.model_copy(update={"max_tool_calls": 1}),
    })})

    async def generate(_binding, _prompt, *, tools, **_options):
        tool = next(tool for tool in tools if tool.name == "get_speaker_list")
        tool.invoke({})
        with pytest.raises(ModelCallBudgetExhaustedError):
            tool.invoke({})
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    with pytest.raises(ModelCallBudgetExhaustedError, match="max_tool_calls"):
        asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas))
    assert not host.state.ready and host.state.closed


def test_voiceprint_source_feedback_can_be_corrected_before_finish(monkeypatch, voiceprint_host):
    host = voiceprint_host
    host.state.coverage = True

    async def generate(_binding, _prompt, *, tools, **_options):
        tools = {tool.name: tool for tool in tools}
        values = {"source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100, "end_ms": 48_800,
                  "selection_basis": "evidence"}
        feedback = json.loads(tools["register_voiceprint"].invoke({**values, "source_ref": "invented"}))
        assert feedback["error"]["code"] == "INVALID_VOICEPRINT_SOURCE"
        tools["register_voiceprint"].invoke(values)
        assert json.loads(tools["finish_voiceprint_preparation"].invoke({"complete": True}))["ready"]
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    assert asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas)).complete
    assert host.state.ready and len(host.state.registrations) == 1


def test_voiceprint_duplicate_returns_progress_without_audio_or_embedding(monkeypatch, voiceprint_host):
    host = voiceprint_host
    host.state.coverage = True
    existing = {
        "added": False, "speaker": "爽世", "refs_count": 2, "target_refs_count": 5,
        "reference": {"source_ref": "source-0", "start_ms": 47_100, "end_ms": 48_800,
                      "vector": [1.0] + [0.0] * 191},
    }

    def precheck(lease, **values):
        assert lease is host.lease
        assert values["model"] == host.encoder.model_name
        assert values["encoder_revision"] == host.encoder.encoder_revision
        assert values["replace_source_ref"] == "source-old"
        return existing

    def embed(_path):
        pytest.fail("A duplicate must return before extracting its embedding")

    async def generate(_binding, _prompt, *, tools, **_options):
        tools = {tool.name: tool for tool in tools}
        result = json.loads(tools["register_voiceprint"].invoke({
            "source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100,
            "end_ms": 48_800, "selection_basis": "evidence",
            "replace_source_ref": "source-old",
        }))
        assert result["added"] is False and result["refs_count"] == 2
        assert result["target_refs_count"] == 5 and result["dimension"] == 192
        assert result["reference"]["source_ref"] == "source-0"
        assert "vector" not in result["reference"]
        assert json.loads(tools["finish_voiceprint_preparation"].invoke({"complete": True}))["ready"]
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime._persistence, "precheck_voiceprint_registration", precheck)
    monkeypatch.setattr(host.encoder, "embed_audio", embed)
    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    assert asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas)).complete
    assert host.state.commands == [] and host.state.registrations == [] and host.state.closed


@pytest.mark.parametrize("failure_at", [None, "encoder", "register"])
def test_voiceprint_replacement_preserves_old_reference_until_embedding_and_commit(
    monkeypatch, voiceprint_host, failure_at,
):
    host = voiceprint_host
    old = {"source_ref": "source-old"}
    references = [old]
    host.source.task.detail["voiceprint"]["voiceprintList"] = [{"speaker": "爽世", "references": references}]
    host.state.coverage = True
    calls = []
    failure = RuntimeError("replacement failed")
    original_embed = host.encoder.embed_audio

    def precheck(_lease, **values):
        assert values["replace_source_ref"] == "source-old" and references == [old]
        calls.append("precheck")

    def embed(path):
        assert references == [old] and calls == ["precheck"]
        calls.append("embed")
        if failure_at == "encoder":
            raise failure
        return original_embed(path)

    def register(_lease, **values):
        assert references == [old] and calls == ["precheck", "embed"]
        assert values["replace_source_ref"] == "source-old"
        calls.append("commit")
        if failure_at == "register":
            raise failure
        references[:] = [values]
        return {"added": True, "replaced_source_ref": "source-old", "reference": values}

    async def generate(_binding, _prompt, *, tools, **_options):
        tools = {tool.name: tool for tool in tools}
        result = json.loads(tools["register_voiceprint"].invoke({
            "source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100,
            "end_ms": 48_800, "selection_basis": "new independent evidence", "replace_source_ref": "source-old",
        }))
        assert result["replaced_source_ref"] == "source-old" and "vector" not in result["reference"]
        assert json.loads(tools["finish_voiceprint_preparation"].invoke({"complete": True}))["ready"]
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime._persistence, "precheck_voiceprint_registration", precheck)
    monkeypatch.setattr(host.runtime._persistence, "register_voiceprint", register)
    monkeypatch.setattr(host.encoder, "embed_audio", embed)
    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    if failure_at:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas))
        assert caught.value is failure and references == [old]
        assert not host.state.ready and host.state.notifications == []
    else:
        assert asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas)).complete
        assert references[0]["source_ref"] == "source-0" and calls == ["precheck", "embed", "commit"]
    assert host.state.closed and all(not path.exists() for path in host.state.audio_paths)


def test_voiceprint_suspect_feedback_requires_explicit_unregister_without_audio(monkeypatch, voiceprint_host, caplog):
    host = voiceprint_host
    caplog.set_level("INFO", logger="server.service.video_parse")
    remaining = {"source-old"}
    suspect = {"source_ref": "source-old", "speaker": "爽世", "start_ms": 47100, "end_ms": 48800,
               "candidate_mean": 0.38}
    original_finish = host.runtime._persistence.finish_voiceprint_preparation
    original_unregister = host.runtime._persistence.unregister_voiceprint

    def finish(_lease, **values):
        result = original_finish(_lease, **values)
        return {**result, "suspect_references": [suspect] if remaining else [],
                "consistency": {"metric": "cosine"}}

    def unregister(_lease, *, source_ref):
        remaining.remove(source_ref)
        host.state.coverage = True
        return original_unregister(_lease, source_ref=source_ref)

    async def generate(_binding, _prompt, *, tools, **_options):
        tools = {tool.name: tool for tool in tools}
        result = json.loads(tools["finish_voiceprint_preparation"].invoke({"complete": True}))
        assert not result["ready"] and result["suspect_references"] == [suspect]
        assert result["consistency"] == {"metric": "cosine"} and remaining == {"source-old"}
        assert host.state.notifications == []
        result = json.loads(tools["unregister_voiceprint"].invoke({"source_ref": "source-old"}))
        assert result["removed"] and result["refs_count"] == 1 and result["target_refs_count"] == 5
        assert json.loads(tools["finish_voiceprint_preparation"].invoke({"complete": True}))["ready"]
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime._persistence, "finish_voiceprint_preparation", finish)
    monkeypatch.setattr(host.runtime._persistence, "unregister_voiceprint", unregister)
    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    assert asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas)).complete
    assert host.state.unregistrations == ["source-old"] and not remaining
    assert host.state.commands == [] and host.state.closed
    assert any("suspects=[('爽世', 'source-old', 47100, 48800, 0.38)]" in record.message
               for record in caplog.records)


@pytest.mark.parametrize("rejected_at", ["precheck_voiceprint_registration", "register_voiceprint"])
@pytest.mark.parametrize("replace_reference", [False, True])
def test_voiceprint_registration_rejection_is_correctable_without_failing_phase(
    monkeypatch, voiceprint_host, rejected_at, replace_reference,
):
    from server.service.video_parse_persistence import VoiceprintRegistrationRejectedError

    host = voiceprint_host
    host.state.coverage = True
    original = getattr(host.runtime._persistence, rejected_at)
    rejected = False

    def reject_once(*args, **values):
        nonlocal rejected
        if not rejected:
            rejected = True
            raise VoiceprintRegistrationRejectedError(
                "VOICEPRINT_OVERLAP", "Requested [47100,48800) overlaps registered [47000,48000).",
                "Choose another independent source with a non-overlapping interval.",
            )
        return original(*args, **values)

    async def generate(_binding, _prompt, *, tools, **_options):
        tools = {tool.name: tool for tool in tools}
        values = {"source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100,
                  "end_ms": 48_800, "selection_basis": "evidence"}
        if replace_reference:
            values["replace_source_ref"] = "source-old"
        feedback = json.loads(tools["register_voiceprint"].invoke(values))["error"]
        assert feedback["code"] == "VOICEPRINT_OVERLAP" and feedback["retryable"] is True
        assert "[47000,48000)" in feedback["message"]
        assert "non-overlapping" in feedback["retry_hint"]
        assert len(host.state.commands) == (0 if rejected_at.startswith("precheck") else 1)
        host.state.manager.check_active()
        tools["register_voiceprint"].invoke({**values, "start_ms": 48_200, "end_ms": 49_900})
        assert json.loads(tools["finish_voiceprint_preparation"].invoke({"complete": True}))["ready"]
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime._persistence, rejected_at, reject_once)
    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    assert asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas)).complete
    assert host.state.ready and host.state.closed and len(host.state.registrations) == 1
    assert len(host.state.commands) == (1 if rejected_at.startswith("precheck") else 2)
    assert all(not path.exists() for path in host.state.audio_paths)


@pytest.mark.parametrize("tool_name", ["register_voiceprint", "unregister_voiceprint"])
def test_voiceprint_normal_model_cancellation_is_not_latched_as_execution_failure(monkeypatch, voiceprint_host, tool_name):
    from gateway.usage import ModelCallCancelledError

    host = voiceprint_host
    cancelled = ModelCallCancelledError("owner cancelled")
    following = RuntimeError("provider cancelled its turn")

    def cancel(*_args, **_values):
        raise cancelled

    async def generate(_binding, _prompt, *, tools, **_options):
        tool = next(tool for tool in tools if tool.name == tool_name)
        with pytest.raises(ModelCallCancelledError) as caught:
            tool.invoke({"source_ref": "source-old"} if tool_name == "unregister_voiceprint" else {
                "source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100,
                "end_ms": 48_800, "selection_basis": "evidence", "replace_source_ref": "source-old",
            })
        assert caught.value is cancelled
        raise following

    if tool_name == "unregister_voiceprint":
        monkeypatch.setattr(host.runtime._persistence, "unregister_voiceprint", cancel)
    else:
        monkeypatch.setattr(host.encoder, "embed_audio", cancel)
    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas))
    assert caught.value is following
    assert not host.state.ready and host.state.closed


def test_voiceprint_failure_cancels_real_manager_but_preserves_original_error(monkeypatch, voiceprint_host):
    from gateway.usage import ModelCallCancelledError

    host = voiceprint_host
    original = TimeoutError("encoder cold start timed out")

    def embed(_path):
        raise original

    async def generate(_binding, _prompt, *, tools, **_options):
        register = next(tool for tool in tools if tool.name == "register_voiceprint")
        with pytest.raises(TimeoutError):
            register.invoke({"source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100,
                             "end_ms": 48_800, "selection_basis": "evidence"})
        # This is the same control used by Gateway's provider polling loop.
        host.state.manager.check_active()

    monkeypatch.setattr(host.encoder, "embed_audio", embed)
    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    with pytest.raises(TimeoutError) as caught:
        asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas))
    assert caught.value is original
    with pytest.raises(ModelCallCancelledError):
        host.state.manager.check_active()
    assert not host.state.ready and host.state.closed


@pytest.mark.parametrize("blocking_provider", [False, True])
def test_voiceprint_failure_before_cancel_keeps_failure_budget_and_pending_ownership(
    monkeypatch, voiceprint_host, blocking_provider,
):
    from threading import Event

    from server.utils.execution import BlockingCallCancelled

    host = voiceprint_host
    original = RuntimeError("encoder failed before cancellation")
    entered, provider_exit = Event(), Event()
    stopped = []
    host.runtime._persistence.stop_voiceprint_preparation = stopped.append

    def embed(_path):
        raise original

    def provider():
        entered.set()
        assert provider_exit.wait(timeout=3)

    async def generate(_binding, _prompt, *, tools, **_options):
        register = next(tool for tool in tools if tool.name == "register_voiceprint")
        with pytest.raises(RuntimeError):
            register.invoke({"source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100,
                             "end_ms": 48_800, "selection_basis": "evidence"})
        if blocking_provider:
            await host.runtime._run_blocking(provider)
        else:
            entered.set()
            await asyncio.Future()

    async def invoke(_task):
        await host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas)

    async def execute():
        task = asyncio.create_task(host.runtime.prepare(host.lease))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(BlockingCallCancelled if blocking_provider else asyncio.CancelledError) as caught:
            await task
        assert caught.value.voiceprint_failed is True and caught.value.__cause__ is original
        assert stopped == [] and not host.state.ready
        if blocking_provider:
            assert host.state.calls.pending and not host.state.closed
            provider_exit.set()
            await asyncio.wrap_future(caught.value.completion)
        assert host.state.closed and not host.state.calls.pending

    monkeypatch.setattr(host.encoder, "embed_audio", embed)
    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    host.runtime.agent = SimpleNamespace(InvokeVideoParse=invoke)
    try:
        asyncio.run(execute())
    finally:
        provider_exit.set()


def test_voiceprint_finish_waits_for_registration_failure_before_committing(monkeypatch, voiceprint_host):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    host = voiceprint_host
    entered, finish_entered, fail_audio = Event(), Event(), Event()
    original = RuntimeError("encoder failed during concurrent finish")
    host.state.coverage = True

    def embed(_path):
        entered.set()
        assert finish_entered.wait(timeout=2)
        assert fail_audio.wait(timeout=2)
        raise original

    async def generate(_binding, _prompt, *, tools, **_options):
        tools = {tool.name: tool for tool in tools}

        def finish():
            finish_entered.set()
            return tools["finish_voiceprint_preparation"].invoke({"complete": True})

        with ThreadPoolExecutor(max_workers=2) as pool:
            registration = pool.submit(tools["register_voiceprint"].invoke, {
                "source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100,
                "end_ms": 48_800, "selection_basis": "evidence",
            })
            assert entered.wait(timeout=2)
            completion = pool.submit(finish)
            assert finish_entered.wait(timeout=2)
            fail_audio.set()
            for future in (registration, completion):
                with pytest.raises(RuntimeError) as caught:
                    future.result(timeout=2)
                assert caught.value is original
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.encoder, "embed_audio", embed)
    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    try:
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas))
        assert caught.value is original
        assert not host.state.ready and host.state.notifications == [] and host.state.closed
    finally:
        fail_audio.set()


def test_voiceprint_notification_failure_preserves_successful_preparation(monkeypatch, voiceprint_host, caplog):
    host = voiceprint_host
    host.state.coverage = True

    def notify(_task):
        raise RuntimeError("notification failed after commit")

    async def generate(_binding, _prompt, *, tools, **_options):
        finish = next(tool for tool in tools if tool.name == "finish_voiceprint_preparation")
        assert json.loads(finish.invoke({"complete": True}))["ready"]
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime, "_notify_change", notify)
    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    assert asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas)).complete
    assert host.state.ready and host.state.closed
    assert "preparation committed; scheduler notification failed" in caplog.text


@pytest.mark.parametrize("changes", [
    {"source_ref": "invented"}, {"speaker": "燈"}, {"start_ms": 46_999}, {"end_ms": 50_001},
    {"audio_path": "/tmp/agent-selected.wav"}, {"vector": [1.0]},
])
def test_voiceprint_registration_rejects_unauthorized_sources_before_audio(monkeypatch, voiceprint_host, changes):
    host = voiceprint_host

    async def generate(_binding, _prompt, *, tools, **_options):
        register = next(tool for tool in tools if tool.name == "register_voiceprint")
        result = json.loads(register.invoke({
            "source_ref": "source-0", "speaker": "爽世", "start_ms": 47_100, "end_ms": 48_800,
            "selection_basis": "evidence", **changes,
        }))
        assert result["error"]["code"] in {"INVALID_VOICEPRINT_SOURCE", "INVALID_ARGUMENTS"}
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    asyncio.run(host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas))
    assert host.state.registrations == [] and host.state.commands == []


@pytest.mark.parametrize("failure", ["encoder", "cancelled"])
def test_voiceprint_temporary_audio_is_removed_when_embedding_does_not_complete(monkeypatch, voiceprint_host, failure):
    host = voiceprint_host
    error = RuntimeError("encoder failed") if failure == "encoder" else asyncio.CancelledError()

    def embed(_path):
        assert Path(_path).exists()
        raise error

    monkeypatch.setattr(host.encoder, "embed_audio", embed)
    with pytest.raises(type(error)):
        host.runtime._embed_speaker_segment(host.encoder, host.source, host.lease,
            start_ms=47_100, end_ms=48_800, check_active=lambda: None)
    assert host.state.audio_paths and all(not path.exists() for path in host.state.audio_paths)


@pytest.mark.parametrize("blocking_provider", [False, True])
@pytest.mark.parametrize("timed_out", [False, True])
def test_voiceprint_timeout_and_cancel_preserve_pending_encoder_ownership(
    monkeypatch, voiceprint_host, blocking_provider, timed_out,
):
    from threading import Event

    from server.utils.execution import BlockingCallCancelled

    host = voiceprint_host
    entered, provider_exit = Event(), Event()
    stopped, released = [], []
    host.runtime._persistence.stop_voiceprint_preparation = stopped.append
    if timed_out:
        config = host.snapshot["inputs"].parse_config
        host.snapshot["inputs"] = host.snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(update={
            "voiceprint": config.voiceprint.model_copy(update={"execution_timeout_seconds": 0.05}),
        })})

    def provider():
        entered.set()
        assert provider_exit.wait(timeout=3)

    async def generate(*_args, **_options):
        if blocking_provider:
            await host.runtime._run_blocking(provider)
        else:
            entered.set()
            await asyncio.Future()

    async def invoke(_task):
        await host.runtime.generate_voiceprint_preparation(host.snapshot, prompt="prepare", canvas=host.canvas)

    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    host.runtime.agent = SimpleNamespace(InvokeVideoParse=invoke)
    consumer = object.__new__(VideoParseConsumer)
    consumer._queue = SimpleNamespace(release_execution=released.append)

    async def execute():
        task = asyncio.create_task(host.runtime.prepare(host.lease))
        assert await asyncio.to_thread(entered.wait, 1)
        if not timed_out:
            task.cancel()
        expected = BlockingCallCancelled if blocking_provider else TimeoutError if timed_out else asyncio.CancelledError
        with pytest.raises(expected) as caught:
            await task
        assert stopped == ([] if timed_out else [host.lease])
        if blocking_provider:
            error = caught.value
            assert error.execution_pending and not error.completion.done()
            assert getattr(error, "voiceprint_failed", False) is timed_out
            assert host.state.calls.pending and not host.state.closed
            await consumer._release_cancelled(host.lease, error)
            assert released == []
            provider_exit.set()
            await asyncio.wrap_future(error.completion)
            assert released == [host.lease] and host.state.closed and not host.state.calls.pending
        else:
            assert host.state.closed

    try:
        asyncio.run(execute())
    finally:
        provider_exit.set()


def test_voiceprint_cancel_before_phase_binding_stops_again_before_consumer_release(tmp_path):
    from threading import Event

    from server.utils.execution import BlockingCallCancelled
    from tests.server.test_video_parse_repository import NOW, _prepare
    from tests.server.test_video_parse_voiceprint import _commit, _enable

    engine = create_database_engine(
        "sqlite+pysqlite:///" + str(tmp_path / "cancel-before-binding.db"),
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    factory, queue, repo, task_id, lease, _prepared = _prepare((engine, None), chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    entered, allow_binding, binding_finished, released = Event(), Event(), Event(), Event()
    observed = []

    def begin(current):
        entered.set()
        assert allow_binding.wait(timeout=3)
        result = repo.begin_voiceprint_preparation(current, now=NOW)
        observed.append("binding committed")
        binding_finished.set()
        return result

    def stop(current):
        result = repo.stop_voiceprint_preparation(current, now=NOW)
        observed.append(("stop", result))
        return result

    def release(current):
        assert binding_finished.is_set()
        with factory() as session:
            phase = session.get(VideoParseTask, task_id).detail[RUNTIME_DETAIL_KEY]["voiceprint_preparation"]
            assert phase["active"] is False
        assert queue.release_execution(current, now=NOW)
        observed.append("consumer released")
        released.set()

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = SimpleNamespace(
        load_task_input=repo.load_task_input, begin_voiceprint_preparation=begin,
        stop_voiceprint_preparation=stop,
    )
    runtime._video_lease = ContextVar("voiceprint-cancel-before-binding-lease", default=None)
    runtime._model_manager = ContextVar("voiceprint-cancel-before-binding-manager", default=None)

    async def invoke(current_task_id):
        await runtime.load_voiceprint_input(current_task_id, LeaseToken(
            owner=lease.worker_id, version=lease.lease_version, expires_at=lease.lease_expire_time,
        ))

    runtime.agent = SimpleNamespace(InvokeVideoParse=invoke)
    consumer = object.__new__(VideoParseConsumer)
    consumer._queue = SimpleNamespace(release_execution=release)

    async def execute():
        task = asyncio.create_task(runtime.prepare(lease))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(BlockingCallCancelled) as caught:
            await task
        assert observed == [("stop", False)]
        assert not caught.value.completion.done()
        await consumer._release_cancelled(lease, caught.value)
        assert not released.is_set()
        allow_binding.set()
        assert await asyncio.to_thread(released.wait, 2)
        assert observed == [("stop", False), "binding committed", ("stop", True), "consumer released"]
        with factory() as session:
            parent = session.get(VideoParseTask, task_id)
            phase = parent.detail[RUNTIME_DETAIL_KEY]["voiceprint_preparation"]
            assert phase["active"] is False and phase.get("failures", 0) == 0
            assert parent.lease_owner is None and parent.last_error is None
            assert parent.detail["voiceprint"]["ready"] is False

    try:
        asyncio.run(execute())
    finally:
        allow_binding.set()
        binding_finished.wait(timeout=3)
        engine.dispose()


def _voice_reference(encoder, speaker, digest, **changes):
    return {
        "speaker": speaker,
        "model": {"name": encoder.model_name, "encoder_revision": encoder.encoder_revision},
        "chunk_id": "reference-chunk", "source_version": 1, "event_fingerprint": "event-" + digest,
        "start_ms": 1000, "end_ms": 2700, "source_start_ms": 1000, "source_end_ms": 3000,
        "vector": [1.0] + [0.0] * 191, "pcm_sha256": digest, "duration_ms": 1700,
        "selection_basis": "named independent dialogue", "confidence": 0.9, **changes,
    }


def _b0_snapshot(host):
    from engine.parsev2.schemas import DialogueEvent

    values = host.snapshot["inputs"].parse_config.model_dump(mode="json")
    values["voiceprint_mode"] = "prefill"
    values["models"].pop("voiceprint")
    inputs = ParseInput.model_validate({"series_id": "series", "video_id": "video", "parse_config": values})
    sources, events = {}, []
    for index, label in enumerate(["A"] * 4 + ["B"] * 3 + ["B", None]):
        start = index * 2000
        end = start + (300 if index == 8 else 1700)
        event = DialogueEvent(
            event_type="DIALOGUE", speaker=label, start_seconds=start, end_seconds=end,
            description="dialogue", participants=[label] if label else [], visible_persons=[],
            text_variants=[{"language": "ja", "role": "ORIGINAL", "text": str(index)}],
        )
        events.append(event)
        source_id = f"chunk:{index}"
        sources[source_id] = {
            "source_id": source_id, "chunk_id": "chunk", "chunk_task_id": "member",
            "position": index, "event_index": index, "source_version": 1,
            "event_fingerprint": hashlib.sha256(str(index).encode()).hexdigest(),
            "speaker": label, "start_ms": start, "end_ms": end,
            "text_variants": event.model_dump(mode="json")["text_variants"],
        }
    return {"inputs": inputs, "video_parse_task_id": host.lease.video_parse_task_id,
            "sources": sources, "member_contents": {"chunk": VideoParseResult(persons=["A", "B"], timeline=events)},
            "member_evidence": {"chunk": {"evidence": {"asr": {"transcript": [
                {"start_seconds": row["start_ms"], "end_seconds": row["end_ms"], "text": str(index)}
                for index, row in enumerate(sources.values())
            ]}}}}, "failed_chunks": [], "timing": {"extra_gate_wait_ms": 1000}}


def _b0_embedding(encoder, *, start_ms, **_options):
    from gateway.speaker_embedding import SpeakerEmbeddingResult

    index = start_ms // 2000
    vector = [0.0, 1.0] if index in {4, 5, 6} else [1.0, 0.0]
    return SpeakerEmbeddingResult(
        model=encoder.model_name, encoder_revision=encoder.encoder_revision,
        vector=vector + [0.0] * 190, duration_ms=1700,
    ), hashlib.sha256(str(index).encode()).hexdigest()


def test_b0_collection_retains_short_lines_and_original_labels_without_mutation(monkeypatch, voiceprint_host):
    from copy import deepcopy

    host = voiceprint_host
    snapshot = _b0_snapshot(host)
    before = deepcopy(snapshot)
    calls = []

    def embed(encoder, _source, _lease, **options):
        calls.append(options["start_ms"])
        return _b0_embedding(encoder, **options)

    monkeypatch.setattr(host.runtime, "_embed_voiceprint_sample", embed)
    result = host.runtime._collect_voiceprint_scores(
        snapshot, host.encoder, host.source, host.lease, check_active=lambda: None,
    )
    assert snapshot == before
    assert len(result["lines"]) == 9 and len(result["samples"]) == 8
    assert len(calls) == 8 and 16000 not in calls
    short = result["lines"][-1]
    assert short["candidates"] == [] and short["gap"] is None
    assert short["reason"] == "audio_too_short" and short["band"] == "NO_VOICE"
    conflict = result["lines"][7]
    assert conflict["understanding_speaker"] == "B"
    assert conflict["band"] == "ACCEPT" and conflict["conflict"] is True
    assert conflict["candidates"][0]["speaker"] == "A"
    assert conflict["candidates"][0]["independent_cue_count"] >= 3
    assert conflict["candidates"][0]["reference_ids"]
    assert all("src" not in candidate for line in result["lines"] for candidate in line["candidates"])
    assert all("text_variants" not in row for row in result["samples"] + result["lines"])
    assert result["timing"]["extra_gate_wait_ms"] == 1000
    assert result["timing"]["preparation_elapsed_ms"] >= 0


def test_b0_song_annotation_prevents_accept(monkeypatch, voiceprint_host):
    host = voiceprint_host
    snapshot = _b0_snapshot(host)
    snapshot["member_contents"]["chunk"].timeline[7].description = "角色唱出歌词"
    monkeypatch.setattr(host.runtime, "_embed_voiceprint_sample",
                        lambda encoder, _source, _lease, **options: _b0_embedding(encoder, **options))
    result = host.runtime._collect_voiceprint_scores(
        snapshot, host.encoder, host.source, host.lease, check_active=lambda: None,
    )
    assert result["lines"][7]["song"] is True
    assert result["lines"][7]["band"] != "ACCEPT"


@pytest.mark.parametrize("outcome", ["complete", "failure", "artifact_failure", "timeout", "cancel", "commit_cancel"])
def test_b0_preparation_publishes_only_complete_results(monkeypatch, voiceprint_host, outcome):
    from gateway.usage import ModelCallCancelledError
    from server.utils.execution import BlockingCallCancelled

    host = voiceprint_host
    snapshot = _b0_snapshot(host)
    if outcome == "timeout":
        config = snapshot["inputs"].parse_config
        snapshot["inputs"] = snapshot["inputs"].model_copy(update={"parse_config": config.model_copy(update={
            "voiceprint": config.voiceprint.model_copy(update={"execution_timeout_seconds": 0.02}),
        })})
    commits = []
    publication = {"locked": False}
    renew = host.runtime._require_video_active

    def renew_outside_publication(lease):
        if publication["locked"]:
            raise AssertionError("renewing the parent would block on the publication row lock")
        return renew(lease)

    monkeypatch.setattr(host.runtime, "_require_video_active", renew_outside_publication)

    async def load(*_args):
        return snapshot

    def collect(*_args, check_active):
        if outcome == "failure":
            raise ValueError("encoder revision changed")
        if outcome in {"timeout", "cancel"}:
            time.sleep(0.1)
            check_active()
        return {"method_version": "test", "lines": ["complete"]}

    def store(*_args, check_active):
        check_active()
        if outcome == "artifact_failure":
            raise OSError("artifact publication failed")
        return {"location": "artifact://complete.json", "checksum": "a" * 64}

    def finish(_lease, *, preparation, artifact, check_active):
        publication["locked"] = True
        try:
            if outcome == "commit_cancel":
                host.runtime._model_manager.get().cancel()
            check_active()
            assert artifact == {"location": "artifact://complete.json", "checksum": "a" * 64}
            commits.append(preparation)
        finally:
            publication["locked"] = False

    monkeypatch.setattr(host.runtime, "load_voiceprint_input", load)
    monkeypatch.setattr(host.runtime, "_collect_voiceprint_scores", collect)
    monkeypatch.setattr(host.runtime, "_store_voiceprint_scoring", store)
    host.runtime._persistence.bind_voiceprint_encoder = lambda _lease, **_options: None
    host.runtime._persistence.finish_voiceprint_scoring = finish
    token = LeaseToken(owner=host.lease.worker_id, version=host.lease.lease_version,
                       expires_at=host.lease.lease_expire_time)

    async def execute():
        task = asyncio.create_task(host.runtime.prepare_voiceprint_scoring(host.lease.video_parse_task_id, token))
        if outcome == "cancel":
            await asyncio.sleep(0.02)
            task.cancel()
        if outcome == "complete":
            await task
            assert commits == [{"method_version": "test", "lines": ["complete"],
                                "video_parse_task_id": host.lease.video_parse_task_id, "video_id": "video"}]
        else:
            expected = (ValueError if outcome == "failure" else OSError if outcome == "artifact_failure"
                        else ModelCallCancelledError if outcome == "commit_cancel"
                        else (TimeoutError, asyncio.CancelledError))
            with pytest.raises(expected) as caught:
                await task
            if isinstance(caught.value, BlockingCallCancelled):
                await asyncio.wrap_future(caught.value.completion)
            assert commits == []
        assert host.state.closed

    asyncio.run(execute())
    assert host.state.notifications == ([host.lease.video_parse_task_id] if outcome == "complete" else [])


def test_b0_compare_reuses_frozen_policy_and_excludes_entire_asr_cue(monkeypatch, voiceprint_host):
    host = voiceprint_host
    snapshot = _b0_snapshot(host)
    monkeypatch.setattr(host.runtime, "_embed_voiceprint_sample",
                        lambda encoder, _source, _lease, **options: _b0_embedding(encoder, **options))
    prepared = host.runtime._collect_voiceprint_scores(
        snapshot, host.encoder, host.source, host.lease, check_active=lambda: None,
    )
    target = prepared["lines"][7]
    excluded = prepared["samples"][0]
    excluded["cue_ids"] = target["cue_ids"]
    result = host.runtime._compare_speaker_b0(
        host.encoder, host.source, host.lease, prepared["samples"], start_ms=14000, end_ms=15700,
        check_active=lambda: None, parameters=prepared["parameters"], lines=prepared["lines"],
    )
    assert result["method_version"] == prepared["method_version"]
    assert result["candidates"][0]["speaker"] == "A"
    assert excluded["id"] not in result["candidates"][0]["reference_ids"]
    assert target["id"] not in result["candidates"][0]["reference_ids"]
    assert result["candidate_margin"] == result["gap"]
    assert not {"band", "conflict", "review_reason"}.intersection(result)


@pytest.mark.parametrize("duration_ms", [1700, 31000])
@pytest.mark.parametrize("invalid", ["revision", "dimension", "normalization"])
def test_b0_actual_encoder_contract_failure_never_publishes(
    monkeypatch, voiceprint_host, duration_ms, invalid,
):
    from gateway.speaker_embedding import SpeakerEmbeddingResult

    host = voiceprint_host
    snapshot = _b0_snapshot(host)
    snapshot["sources"]["chunk:0"]["end_ms"] = duration_ms
    commits = []

    def embed(_path):
        return SpeakerEmbeddingResult(
            model=host.encoder.model_name,
            encoder_revision="different revision" if invalid == "revision" else host.encoder.encoder_revision,
            vector=([1.0] + [0.0] * 190 if invalid == "dimension" else
                    [2.0 if invalid == "normalization" else 1.0] + [0.0] * 191),
            duration_ms=1700,
        )

    async def load(*_args):
        return snapshot

    monkeypatch.setattr(host.encoder, "embed_audio", embed)
    monkeypatch.setattr(host.runtime, "load_voiceprint_input", load)
    host.runtime._persistence.bind_voiceprint_encoder = lambda _lease, **_options: None
    host.runtime._persistence.finish_voiceprint_scoring = lambda *_args, **options: commits.append(options)
    token = LeaseToken(owner=host.lease.worker_id, version=host.lease.lease_version,
                       expires_at=host.lease.lease_expire_time)
    with pytest.raises(ValueError, match="contract|normalized"):
        asyncio.run(host.runtime.prepare_voiceprint_scoring(host.lease.video_parse_task_id, token))
    assert commits == [] and host.state.closed
    assert not any(path.exists() for path in host.state.audio_paths)


def test_b0_member_evidence_is_loaded_before_actual_refine_prompt(monkeypatch, voiceprint_host):
    from agent.video_parse_v2.refine.agent import IntervalRefine
    from server.model.dto.jobDTO import IntervalParseLease

    host = voiceprint_host
    prepared_snapshot = _b0_snapshot(host)
    monkeypatch.setattr(host.runtime, "_embed_voiceprint_sample",
                        lambda encoder, _source, _lease, **options: _b0_embedding(encoder, **options))
    prepared = host.runtime._collect_voiceprint_scores(
        prepared_snapshot, host.encoder, host.source, host.lease, check_active=lambda: None,
    )
    source = SimpleNamespace(
        duration_seconds=60_000, background={}, series_id="series",
        task=SimpleNamespace(video_id="video", chunk_count=1,
                             parse_config=prepared_snapshot["inputs"].parse_config.model_dump(mode="json")),
    )
    content = prepared_snapshot["member_contents"]["chunk"].model_dump(mode="json")
    member = SimpleNamespace(chunk_parse_task_id="member", sequence_no=0, version=1,
                             detail={}, content=content)
    lease = IntervalParseLease(str(uuid4()), host.lease.video_parse_task_id, ("member",), 0, 1,
                               "REFINING", "worker", 2, datetime.now(UTC), 0, 3, 1.0, 2.0)
    interval = SimpleNamespace(interval_parse_task_id=lease.interval_parse_task_id,
                               video_parse_task_id=lease.video_parse_task_id,
                               start_sequence_no=0, chunk_count=1, members=(member,))
    host.runtime._interval_lease = ContextVar("b0-handoff-interval", default=lease)
    host.runtime._persistence.load_interval_members = lambda _lease: interval
    host.runtime._persistence.load_task_input = lambda _task_id: source
    async def load_scoring(_lease):
        return prepared

    monkeypatch.setattr(host.runtime, "_load_voiceprint_scoring", load_scoring)
    committed = []

    async def generate(snapshot, *, prompt, canvas):
        assert snapshot["member_evidence"]["member"]["voiceprint"]["lines"] == prepared["lines"]
        evidence = snapshot["member_evidence"]["member"]["voiceprint"]
        for line in evidence["lines"]:
            for candidate in line["candidates"]:
                assert all(reference_id in evidence["samples"] for reference_id in candidate["reference_ids"])
        assert all("vector" not in row and "text_variants" not in row for row in evidence["samples"].values())
        target = canvas.items("events")[7][1]
        assert target["event"]["speaker"] == "A"
        assert target["event"]["speaker_source"] == "voice_default"
        assert target["voice_evidence"]["understanding_speaker"] == "B"
        assert "ACCEPT" in prompt and "event_ref" in prompt
        assert snapshot["member_contents"]["member"].timeline[7].speaker == "B"
        canvas.write(role="refine", batch_id="resolve-voice", expected_revision=canvas.revision, operations=[{
            "op": "voice_decision", "event_ref": canvas.items("events")[7][0],
            "decision": "voice_top1", "reason": "Voice evidence supports the prepared speaker.",
        }])
        assert canvas.finish_voiceprint_review()["ready"]
        return CanvasCompletion(complete=True)

    async def commit(snapshot, result, *, voiceprint_decisions):
        committed.append((snapshot, result, voiceprint_decisions))
        return True

    async def notify(_snapshot):
        pass

    monkeypatch.setattr(host.runtime, "generate_interval_refinement", generate)
    monkeypatch.setattr(host.runtime, "commit_interval_refinement", commit)
    monkeypatch.setattr(host.runtime, "notify_interval_committed", notify)
    asyncio.run(IntervalRefine(runtime=host.runtime).InvokeRefine(lease.interval_parse_task_id))
    assert len(committed) == 1
    assert committed[0][1].updates[0].timeline[7].speaker == "A"
    assert committed[0][2][0]["decision"] == "voice_top1"
    assert committed[0][2][0]["final_speaker"] == "A"


def test_b0_compare_tool_keeps_large_provenance_in_paginated_evidence(monkeypatch, voiceprint_host):
    from server.model.dto.jobDTO import IntervalParseLease

    host = voiceprint_host
    snapshot = _b0_snapshot(host)
    samples = []
    for index in range(200):
        name = "A" if index < 100 else "B"
        samples.append({
            "id": f"ref-{index}", "source_id": f"ref-{index}", "chunk_id": "chunk",
            "source_version": 1, "event_fingerprint": hashlib.sha256(f"event{index}".encode()).hexdigest(),
            "start_ms": index * 100, "end_ms": index * 100 + 500,
            "cue_ids": [f"cue-{index}"], "label": name, "song": False, "overlap": False,
            "pcm_sha256": hashlib.sha256(str(index).encode()).hexdigest(),
            "model": host.encoder.model_name, "encoder_revision": host.encoder.encoder_revision,
            "vector": ([1.0, 0.0] if name == "A" else [0.0, 1.0]) + [0.0] * 190,
            "text_variants": [{"text": "source provenance " * 30}],
        })
    prepared = {"samples": samples, "lines": samples,
                "parameters": host.runtime._voiceprint_parameters(),
                "model": {"name": host.encoder.model_name, "encoder_revision": host.encoder.encoder_revision}}
    lease = IntervalParseLease(str(uuid4()), host.lease.video_parse_task_id, ("member",), 0, 1,
                               "REFINING", "worker", 2, datetime.now(UTC), 0, 3, 1.0, 2.0)
    async def load_scoring(_lease):
        return prepared

    monkeypatch.setattr(host.runtime, "_load_voiceprint_scoring", load_scoring)
    monkeypatch.setattr(host.runtime, "_require_interval_token", lambda *_args, **_options: lease)
    monkeypatch.setattr(host.runtime, "_require_interval_active", lambda _lease: None)
    canvas = ParseContextCanvas(scope="refine:b0-budget", members={"member": {
        "sequence_no": 0, "version": 1, "bounds": (0, 60000),
        "content": VideoParseResult(persons=[], timeline=[]),
    }})
    canvas.register_evidence("member", {"voiceprint": {"mode": "prefill", "lines": []}})
    canvas._apply_voiceprint()
    refine_input = {"inputs": snapshot["inputs"], "video_parse_task_id": lease.video_parse_task_id,
                    "interval_parse_task_id": lease.interval_parse_task_id, "lease": object(),
                    "member_bounds": {"member": (0, 60000)}}

    async def generate(_binding, _prompt, *, tools, **_options):
        compare = next(tool for tool in tools if tool.name == "compare_speaker")
        content = compare.invoke({"start_ms": 47100, "end_ms": 48800})
        result = json.loads(content)
        assert len(content.encode()) < 32000
        assert result["candidates"][0]["speaker"] == "A"
        assert result["revision"] == canvas.revision
        assert "src" not in result["candidates"][0] and "reference_ids" not in result["candidates"][0]
        full = canvas.get("evidence", result["evidence_ref"])[result["evidence_section"]]
        assert len(json.dumps(full).encode()) > 32000
        assert len(full["candidates"][0]["reference_ids"]) == 80
        assert all(reference_id in full["samples"] for reference_id in full["candidates"][0]["reference_ids"])
        assert all("vector" not in ref and "text_variants" not in ref for ref in full["samples"].values())
        assert not {"band", "conflict", "review_reason"}.intersection(result)
        assert not {"band", "conflict", "review_reason"}.intersection(full)
        finish = next(tool for tool in tools if tool.name == "finish_voiceprint_review")
        assert json.loads(finish.invoke({"complete": True}))["ready"]
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    asyncio.run(host.runtime.generate_interval_refinement(refine_input, prompt="refine", canvas=canvas))


@pytest.mark.parametrize("damage", [None, "bytes", "task", "counts", "model"])
def test_b0_artifact_roundtrip_and_integrity_checks(monkeypatch, voiceprint_host, tmp_path, damage):
    from server.storage.artifact import ArtifactIntegrityError, LocalArtifactStore

    host = voiceprint_host
    source = SimpleNamespace(series_id=str(uuid4()), task=SimpleNamespace(video_id=str(uuid4())))
    host.runtime._artifact_store = LocalArtifactStore(tmp_path / "durable", environment="test")
    preparation = {"video_parse_task_id": host.lease.video_parse_task_id, "video_id": source.task.video_id,
                   "method_version": "B0-test", "model": {"name": "campplus", "encoder_revision": "frozen"},
                   "parameters": {"trim_fraction": 0.2}, "lines": [], "samples": []}
    artifact = host.runtime._store_voiceprint_scoring(source, host.lease, preparation, check_active=lambda: None)
    manifest = {"ready": True, **artifact, **{key: preparation[key] for key in ("method_version", "model", "parameters")},
                "line_count": 0, "sample_count": 0}
    host.runtime._persistence.load_voiceprint_scoring = lambda _lease: manifest
    assert asyncio.run(host.runtime._load_voiceprint_scoring(host.lease)) == preparation
    path = host.runtime._artifact_store.resolve(artifact["location"])
    if damage == "bytes":
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "task":
        preparation["video_parse_task_id"] = str(uuid4())
        changed = host.runtime._store_voiceprint_scoring(source, host.lease, preparation, check_active=lambda: None)
        manifest.update(changed)
    elif damage == "counts":
        manifest["line_count"] = 1
    elif damage == "model":
        manifest["model"] = {"name": "different", "encoder_revision": "frozen"}
    if damage is not None:
        with pytest.raises(ArtifactIntegrityError):
            asyncio.run(host.runtime._load_voiceprint_scoring(host.lease))
    assert len(json.dumps(manifest).encode()) < 1000


def test_b0_published_artifact_manifest_and_refine_load_use_same_bytes(repository, voiceprint_host, tmp_path, monkeypatch):
    from server.storage.artifact import LocalArtifactStore
    from tests.server.test_video_parse_repository import NOW, _prepare
    from tests.server.test_video_parse_voiceprint import _commit, _enable_scoring

    factory, queue, repo, task_id, lease, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A", None], 0)
    snapshot = repo.begin_voiceprint_preparation(lease, now=NOW)
    snapshot["member_contents"] = {key: VideoParseResult.model_validate(value)
                                   for key, value in snapshot["member_contents"].items()}
    runtime = voiceprint_host.runtime
    runtime._artifact_store = LocalArtifactStore(tmp_path / "durable", environment="test")
    monkeypatch.setattr(runtime, "_embed_voiceprint_sample",
                        lambda encoder, _source, _lease, **options: _b0_embedding(encoder, **options))
    source = repo.load_task_input(task_id)
    preparation = runtime._collect_voiceprint_scores(
        snapshot, voiceprint_host.encoder, source, lease, check_active=lambda: None,
    )
    preparation.update(video_parse_task_id=task_id, video_id=source.task.video_id)
    artifact = runtime._store_voiceprint_scoring(source, lease, preparation, check_active=lambda: None)
    assert repo.load_voiceprint_scoring(lease, now=NOW) is None
    manifest = repo.finish_voiceprint_scoring(lease, preparation=preparation, artifact=artifact, now=NOW)
    with factory() as session:
        stored = session.get(VideoParseTask, task_id).detail["voiceprint"]
        assert stored == manifest
        assert "samples" not in stored and "lines" not in stored and "scoring" not in stored
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    assert refine is not None
    assert runtime._read_voiceprint_scoring(refine, repo.load_voiceprint_scoring(refine, now=NOW)) == preparation


def test_compare_speaker_excludes_self_overlap_duplicate_and_encoder_mismatch(voiceprint_host):
    from server.model.dto.jobDTO import IntervalParseLease

    host = voiceprint_host
    lease = IntervalParseLease(str(uuid4()), host.lease.video_parse_task_id, (), 0, 1,
        "REFINING", "worker", 2, datetime.now(UTC), 0, 3, 1.0, 2.0)
    target_hash = hashlib.sha256(b"\x01\x00" * 27200).hexdigest()
    references = [
        _voice_reference(host.encoder, "爽世", "soyo"),
        _voice_reference(host.encoder, "爽世", "soyo", chunk_id="duplicated-chunk"),
        _voice_reference(host.encoder, "燈", "tomori", vector=[0.999, 0.03] + [0.0] * 190),
        _voice_reference(host.encoder, "target-self", "self", start_ms=47_100, end_ms=48_800),
        _voice_reference(host.encoder, "overlapping-event", "overlap", start_ms=45_000, end_ms=46_000,
                         source_start_ms=45_000, source_end_ms=50_000),
        _voice_reference(host.encoder, "same-target-pcm", target_hash),
        _voice_reference(host.encoder, "conflicting-A", "ambiguous-pcm"),
        _voice_reference(host.encoder, "conflicting-B", "ambiguous-pcm"),
        _voice_reference(host.encoder, "other-encoder", "old", model={"name": "old-model", "encoder_revision": "old"}),
    ]
    used = []
    result = host.runtime._compare_speaker(host.encoder, host.source, lease, references, used,
        start_ms=47_100, end_ms=48_800, check_active=lambda: None)
    assert [candidate["speaker"] for candidate in result["candidates"]] == ["爽世", "燈"]
    assert all(candidate["reference_count"] == 1 for candidate in result["candidates"])
    assert 0 < result["candidate_margin"] < 0.01
    assert result["similarity_metric"] == "cosine" and result["similarity_is_probability"] is False
    assert result["quality"]["duration_ms"] == 1700 and result["quality"]["pcm_sha256"] == target_hash
    assert used == [references[0], references[2]]
    provenance = result["candidates"][0]["references"][0]
    assert provenance["chunk_id"] == "reference-chunk" and provenance["source_version"] == 1
    assert provenance["event_fingerprint"] == "event-soyo" and "vector" not in provenance
    assert all(not path.exists() for path in host.state.audio_paths)


def test_compare_speaker_without_eligible_references_does_not_extract_audio(monkeypatch, voiceprint_host):
    host = voiceprint_host
    monkeypatch.setattr(host.runtime, "_embed_speaker_segment", lambda *_args, **_kwargs: pytest.fail("no eligible references"))
    result = host.runtime._compare_speaker(host.encoder, host.source, host.lease, [], [],
        start_ms=47_100, end_ms=48_800, check_active=lambda: None)
    assert result["candidates"] == [] and result["candidate_margin"] is None
    assert result["quality"]["reason"] == "no_eligible_reference"


def test_refine_compare_tool_tracks_references_for_diagnostics_only(monkeypatch, voiceprint_host):
    from server.model.dto.jobDTO import IntervalParseLease

    host = voiceprint_host
    lease = IntervalParseLease(str(uuid4()), host.lease.video_parse_task_id, ("member",), 0, 1,
        "REFINING", "worker", 2, datetime.now(UTC), 0, 3, 1.0, 2.0)
    reference = _voice_reference(host.encoder, "爽世", "soyo")
    host.runtime._persistence.load_voiceprint_references = lambda _lease: [reference]
    committed = []
    host.runtime._persistence.commit_interval_refinement = lambda _lease, **values: committed.append(values)
    monkeypatch.setattr(host.runtime, "_require_interval_token", lambda *_args, **_kwargs: lease)
    monkeypatch.setattr(host.runtime, "_require_interval_active", lambda _lease: None)
    canvas = ParseContextCanvas(scope="refine:voice", members={"member": {
        "sequence_no": 0, "version": 1, "bounds": (40_000, 60_000),
        "content": VideoParseResult(persons=[], timeline=[]),
    }})
    snapshot = {
        "inputs": host.snapshot["inputs"], "video_parse_task_id": host.lease.video_parse_task_id,
        "interval_parse_task_id": lease.interval_parse_task_id, "lease": object(),
        "member_bounds": {"member": (40_000, 60_000)}, "member_sequence_nos": {"member": 0},
        "member_versions": {"member": 1}, "member_fingerprints": {"member": "unchanged"},
        "member_contents": {"member": VideoParseResult(persons=[], timeline=[])},
        "member_scopes": {"member": [0]},
    }

    async def generate(_binding, _prompt, *, tools, **_options):
        compare = next(tool for tool in tools if tool.name == "compare_speaker")
        rejected = json.loads(compare.invoke({"start_ms": 10_000, "end_ms": 11_000}))
        assert rejected["error"]["code"] == "INVALID_VOICEPRINT_TARGET"
        result = json.loads(compare.invoke({"start_ms": 47_100, "end_ms": 48_800}))
        assert result["candidates"][0]["speaker"] == "爽世"
        return CanvasCompletion(complete=True)

    monkeypatch.setattr(host.runtime, "_generate_text_structured", generate)
    asyncio.run(host.runtime.generate_interval_refinement(snapshot, prompt="refine", canvas=canvas))
    assert host.state.closed and snapshot["voiceprint_references"] == [reference]
    canvas.write(role="refine", batch_id="refined-event", expected_revision=canvas.revision, operations=[{
        "op": "add_event", "member_ref": "member", "key": "action",
        "event": {"event_type": "ACTION", "start_seconds": 47_100, "end_seconds": 48_800,
                  "description": "corrected action", "participants": [], "visible_persons": []},
    }])
    assert asyncio.run(host.runtime.commit_interval_refinement(snapshot, canvas.interval_result()))
    assert "voiceprint_references" not in committed[0]
    assert committed[0]["input_contents"] == {"member": {"persons": [], "timeline": []}}
    assert committed[0]["member_updates"]["member"]["timeline"][0]["description"] == "corrected action"


@pytest.mark.parametrize("structured", [False, True])
def test_refine_commit_conflict_logs_reason_and_safe_context(monkeypatch, voiceprint_host, caplog, structured):
    from server.model.dto.jobDTO import IntervalParseLease
    from server.service.video_parse_persistence import VideoParseSnapshotConflictError

    host = voiceprint_host
    lease = IntervalParseLease(str(uuid4()), host.lease.video_parse_task_id, ("member",), 0, 1,
        "REFINING", "worker", 2, datetime.now(UTC), 0, 3, 1.0, 2.0)
    context = {"chunk_id": "member-chunk", "current_changed_fields": ["description"]} if structured else {}
    error = VideoParseSnapshotConflictError("Refine changed the same event differently", **(
        {"reason": "event_changed", "context": context} if structured else {}
    ))

    def reject(_lease, **_values):
        assert _lease is lease
        raise error

    host.runtime._persistence.commit_interval_refinement = reject
    monkeypatch.setattr(host.runtime, "_require_interval_token", lambda *_args, **_kwargs: lease)
    canvas = ParseContextCanvas(scope="refine:conflict", members={"member": {
        "sequence_no": 0, "version": 1, "bounds": (40_000, 60_000),
        "content": VideoParseResult(persons=[], timeline=[]),
    }})
    reference = _voice_reference(host.encoder, "爽世", "soyo", selection_basis="private-selection-basis")
    reference["text"] = "private-dialogue-text"
    snapshot = {
        "inputs": host.snapshot["inputs"], "video_parse_task_id": host.lease.video_parse_task_id,
        "interval_parse_task_id": lease.interval_parse_task_id, "lease": object(),
        "member_sequence_nos": {"member": 0}, "member_versions": {"member": 1},
        "member_fingerprints": {"member": "unchanged"}, "member_scopes": {"member": [0]},
        "member_contents": {"member": VideoParseResult(persons=[], timeline=[])},
        "voiceprint_references": [reference],
    }

    with caplog.at_level("INFO", logger="server.service.video_parse"):
        assert asyncio.run(host.runtime.commit_interval_refinement(snapshot, canvas.interval_result())) is False
    message = next(record.getMessage() for record in caplog.records if "event=refine_commit_conflict" in record.getMessage())
    assert f"task={lease.video_parse_task_id}" in message and "video=video" in message
    assert f"interval {lease.interval_parse_task_id}" in message and "lease=2" in message
    assert f"reason={error.reason}" in message
    details = json.loads(message.split(" details=", 1)[1])
    assert details == {"message": str(error), "context": context, "input_versions": {"member": 1},
                       "updated_members": [], "used_reference_count": 1}
    assert all(value not in message for value in ("vector", "private-dialogue-text", "private-selection-basis"))


def test_refinement_reuses_understanding_output_budget_and_reasoning(monkeypatch):
    from unittest.mock import AsyncMock

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._model_manager = ContextVar("budget-manager", default=None)
    generated = AsyncMock(return_value=CanvasCompletion(complete=True))
    monkeypatch.setattr(runtime, "_generate_text_structured", generated)
    config = default_video_parse_v2_config()
    config = config.model_copy(update={"models": config.models.model_copy(update={
        "understanding": config.models.understanding.model_copy(update={
            "max_output_tokens": 65536, "reasoning_effort": "none", "supports_vision_input": False,
        }),
    })})
    assert config.models.refine is None

    asyncio.run(runtime.generate_interval_refinement(
        {"inputs": SimpleNamespace(parse_config=config)}, prompt="refine",
        canvas=ParseContextCanvas(scope="refine:budget", members={"member": {
            "sequence_no": 0, "version": 1, "bounds": (0, 20_000),
            "content": VideoParseResult(persons=[], timeline=[]),
        }}),
    ))

    binding = generated.await_args.args[0]
    assert generated.await_args.kwargs["tool_harness"] == "deepagents"
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
        assert options.get("tool_harness", "legacy") == "legacy"
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
    runtime._persistence = SimpleNamespace(
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
@pytest.mark.parametrize("enforce_model_call_limit", [False, True])
def test_canvas_channel_preserves_output_budget_and_uses_valid_policy(monkeypatch, max_output_tokens, enforce_model_call_limit):
    captured = {}

    class Model:
        def __init__(self, model, **options):
            captured["constructed"] = options

        def generate(self, _inputs, **options):
            captured["generated"] = options
            return CanvasCompletion(complete=True)

    monkeypatch.setenv("ARK_API_KEY", "fake-key")
    monkeypatch.setattr("gateway.ark.multimodal.ArkMultimodalModel", Model)
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway_config = load_config()
    runtime._gateway = GatewayClient(runtime._gateway_config)
    binding = GenerativeModelConfig(source="ark_default", model="model", request_timeout_seconds=60,
                                    max_output_tokens=max_output_tokens, reasoning_effort="none",
                                    context_window_tokens=262144 if max_output_tokens == 131072 else 128000)
    expected = runtime._gateway_config.resolve_model_binding(
        ModelBinding.model_validate(binding.model_dump(exclude_none=True)),
    )
    checked = []
    check_control = lambda: checked.append("checked")
    context_checkpoint = lambda: []
    result = runtime._invoke_multimodal(binding, [TextInput("refine")], schema_name="CanvasCompletion", result_type=CanvasCompletion,
                                       tools=(_read_context_tool(),), max_model_calls=100,
                                       check_control=check_control, context_checkpoint=context_checkpoint,
                                       enforce_model_call_limit=enforce_model_call_limit)
    assert result.complete
    assert captured["constructed"]["max_output_tokens"] == expected.max_output_tokens
    assert captured["constructed"]["reasoning_effort"] == "none"
    policy = captured["generated"]["context_window_policy"]
    assert policy.output_reserve_tokens == expected.max_output_tokens
    assert policy.auto_compact_tokens < policy.maximum_input_tokens
    assert policy.compact_target_tokens < policy.auto_compact_tokens
    assert captured["generated"]["max_input_chars"] == expected.max_input_chars
    assert captured["generated"]["max_model_calls"] == (100 if enforce_model_call_limit else None)
    assert captured["generated"]["request_timeout_seconds"] == 60
    checked.clear()
    captured["generated"]["check_control"]()
    assert checked == ["checked"]
    assert captured["generated"]["context_checkpoint"] is context_checkpoint
    assert not runtime._gateway._resources
    runtime._gateway.close()


@pytest.mark.parametrize("blocking_provider", [False, True])
def test_prefix_cancellation_releases_lease_only_after_execution_exits(monkeypatch, blocking_provider):
    from threading import Event
    from server.utils.execution import run_blocking

    released = []
    retried = []
    entered, provider_exit, released_event = Event(), Event(), Event()

    class _Repository:
        @staticmethod
        def load_task_input(_task_id):
            return object()

        @staticmethod
        def release_prefix(lease):
            released.append(lease)
            released_event.set()
            return True

        @staticmethod
        def retry_prefix(*args, **kwargs):
            retried.append((args, kwargs))
            return True

    started = asyncio.Event()

    def provider():
        entered.set()
        assert provider_exit.wait(timeout=3)

    class _PrefixAgent:
        @staticmethod
        async def InvokePrefix(_task_id):
            started.set()
            if blocking_provider:
                await run_blocking(provider)
            else:
                await asyncio.Future()

    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = _Repository()
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
        if blocking_provider:
            assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if blocking_provider:
            assert released == [] and retried == []
            provider_exit.set()
            assert await asyncio.to_thread(released_event.wait, 1)

    try:
        asyncio.run(execute())
    finally:
        provider_exit.set()
    assert released == [lease]
    assert retried == []


@pytest.mark.parametrize("initial_receipt", [True, False, None])
def test_model_stage_accepts_post_commit_heartbeat_loss(monkeypatch, initial_receipt):
    from threading import Event
    from server.model.dto.jobDTO import ChunkParseLease

    managers = []
    first_check = Event()
    samples = []

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
            if self.checked == 1:
                first_check.set()
                if initial_receipt is None:
                    raise RuntimeError("receipt temporarily unavailable")
                return initial_receipt
            return True

    monkeypatch.setattr(
        "server.service.video_parse.VideoParseModelCallManager",
        _Manager,
    )
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = SimpleNamespace(
        record_execution_timing=lambda _lease, **sample: samples.append(sample) or True,
    )
    runtime._queue = _Queue()
    runtime._lease_duration = timedelta(seconds=1)
    runtime._heartbeat_interval = timedelta(milliseconds=1)
    runtime._parse_usage = None
    runtime._tracing = None
    runtime._model_manager = ContextVar("test_model_manager", default=None)
    lease = ChunkParseLease(
        chunk_parse_task_id=str(uuid4()),
        video_parse_task_id=str(uuid4()),
        video_chunk_id=str(uuid4()),
        worker_id="worker",
        lease_version=1,
        lease_expire_time=datetime.now(UTC) + timedelta(seconds=1),
        retry_count=0,
        max_retry_count=3,
        retry_initial_delay_seconds=1,
        retry_max_delay_seconds=5,
    )

    async def execute():
        async with runtime._model_stage(
            lease,
            None,  # type: ignore[arg-type]
            sequence_no=None,
            category="parse",
            check_active=lambda: None,
        ):
            assert await asyncio.to_thread(first_check.wait, 1)

    asyncio.run(execute())
    assert runtime._queue.checked == (1 if initial_receipt else 2)
    assert managers[0].cancel_count == (1 if initial_receipt else 2)
    assert samples[-1]["finished"] is True and samples[-1]["outcome"] == "completed"


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
    runtime._persistence = object()
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


@pytest.mark.parametrize("kind", ["chunk", "refine", "index", "video"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
def test_execution_timing_records_short_runs_without_changing_results(monkeypatch, kind, outcome):
    from server.model.dto.jobDTO import ChunkParseLease, IntervalParseLease

    samples = []
    clock = [10.0]

    class Manager:
        def __init__(self, *_args, **_kwargs):
            pass

        def capture(self):
            return nullcontext()

        def cancel(self):
            pass

    monkeypatch.setattr("server.service.video_parse.VideoParseModelCallManager", Manager)
    monkeypatch.setattr("server.service.video_parse.time", SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=lambda: 1_800_000_000_000_000_000,
    ))
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = SimpleNamespace(record_execution_timing=lambda lease, **sample: samples.append((lease, sample)) or True)
    runtime._queue = object()
    runtime._lease_duration = timedelta(seconds=5)
    runtime._heartbeat_interval = timedelta(seconds=1)
    runtime._parse_usage = runtime._tracing = None
    runtime._model_manager = ContextVar("timing_test_manager", default=None)
    if kind == "chunk":
        lease = ChunkParseLease("chunk", "task", "media", "worker", 3, datetime.now(UTC), 1, 3, 5.0, 300.0)
    elif kind == "index":
        lease = ChunkParseLease("chunk", "task", "media", "worker", 3, datetime.now(UTC), 1, 3, 5.0, 300.0, phase=3)
    elif kind == "refine":
        lease = IntervalParseLease("interval", "task", ("chunk",), 0, 1,
            "REFINING", "worker", 3, datetime.now(UTC), 1, 3, 5.0, 300.0)
    else:
        lease = VideoParseLease("task", "video", "worker", 3, datetime.now(UTC), 1, 3, {})
    error = RuntimeError("business failure") if outcome == "failed" else asyncio.CancelledError()

    async def execute():
        async with runtime._model_stage(lease, None, sequence_no=None,
                category="embedding" if kind == "index" else "parse", check_active=lambda: None):
            clock[0] += 0.025
            if outcome != "completed":
                raise error

    if outcome == "completed":
        asyncio.run(execute())
    else:
        with pytest.raises(type(error)) as caught:
            asyncio.run(execute())
        assert caught.value is error
    assert runtime._model_manager.get() is None
    if kind == "video":
        assert samples == []
    else:
        assert len(samples) == 2
        assert all(saved_lease is lease for saved_lease, _sample in samples)
        assert samples[0][1]["elapsed_ms"] == 0
        final = samples[-1][1]
        assert final["finished"] is True and final["outcome"] == outcome
        assert 24 <= final["elapsed_ms"] <= 25
        assert final["started_at_ms"] == samples[0][1]["started_at_ms"]


def test_execution_timing_heartbeats_and_storage_errors_preserve_business_work(monkeypatch, caplog):
    from server.model.dto.jobDTO import ChunkParseLease

    samples, managers = [], []
    clock = [10.0]
    loop = None
    sampled = None

    def record(_lease, **sample):
        samples.append(sample)
        if len(samples) == 2:
            loop.call_soon_threadsafe(sampled.set)
            raise RuntimeError("timing storage unavailable")
        return True

    class Manager:
        def __init__(self, *_args, **_kwargs):
            self.cancelled = False
            managers.append(self)

        def capture(self):
            return nullcontext()

        def heartbeat(self):
            clock[0] += 0.1
            return True

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr("server.service.video_parse.VideoParseModelCallManager", Manager)
    monkeypatch.setattr("server.service.video_parse.time", SimpleNamespace(
        monotonic=lambda: clock[0], time_ns=lambda: 1_800_000_000_000_000_000,
    ))
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = SimpleNamespace(record_execution_timing=record)
    runtime._queue = object()
    runtime._lease_duration = timedelta(seconds=5)
    runtime._heartbeat_interval = timedelta(milliseconds=1)
    runtime._parse_usage = runtime._tracing = None
    runtime._model_manager = ContextVar("timing_heartbeat_test_manager", default=None)
    lease = ChunkParseLease("chunk", "task", "media", "worker", 1, datetime.now(UTC), 0, 3, 5.0, 300.0)

    async def execute():
        nonlocal loop, sampled
        loop, sampled = asyncio.get_running_loop(), asyncio.Event()
        async with runtime._model_stage(lease, None, sequence_no=None,
                category="parse", check_active=lambda: None):
            await asyncio.wait_for(sampled.wait(), timeout=1)

    asyncio.run(execute())
    assert len(samples) >= 3
    assert samples[-1]["finished"] is True
    assert samples[-1]["outcome"] == "completed"
    assert samples[-1]["elapsed_ms"] >= 99
    assert managers[0].cancelled
    assert "execution timing append failed" in caplog.text


def test_long_calls_leave_the_database_control_executor_available():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event
    from server.utils.execution import run_blocking

    entered, release = Barrier(13), Event()
    context = ContextVar("isolated_execution_context", default="missing")

    def long_call():
        entered.wait(timeout=3)
        assert release.wait(timeout=3)
        return context.get()

    async def execute():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        context.set("preserved")
        calls = [asyncio.create_task(run_blocking(long_call)) for _ in range(12)]
        try:
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), timeout=4)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "heartbeat"), timeout=0.5) == "heartbeat"
        finally:
            release.set()
        assert await asyncio.gather(*calls) == ["preserved"] * 12

    asyncio.run(execute())


def test_cancelled_blocking_call_retains_resources_until_the_thread_exits():
    from threading import Event
    from server.utils.execution import (
        BlockingCallCancelled, ExecutionCalls, cleanup_execution, execution_calls, run_blocking,
    )

    entered, release, cleaned = Event(), Event(), Event()

    def long_call():
        entered.set()
        assert release.wait(timeout=3)

    async def execute():
        calls = ExecutionCalls()
        token = execution_calls.set(calls)
        try:
            task = asyncio.create_task(run_blocking(long_call))
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(BlockingCallCancelled) as caught:
                await asyncio.wait_for(task, timeout=0.5)
            assert caught.value.execution_pending and not caught.value.completion.done()
            await cleanup_execution(cleaned.set)
            assert calls.pending and not cleaned.is_set()
            release.set()
            assert await asyncio.to_thread(cleaned.wait, 1)
            assert not calls.pending
        finally:
            release.set()
            execution_calls.reset(token)

    asyncio.run(execute())


def test_cancelled_parallel_calls_keep_the_lease_until_every_call_exits():
    from threading import Event
    from server.utils.execution import BlockingCallCancelled, ExecutionCalls

    entered = [Event(), Event()]
    release = [Event(), Event()]

    def long_call(index):
        entered[index].set()
        assert release[index].wait(timeout=3)

    async def execute():
        calls = ExecutionCalls()
        tasks = [asyncio.create_task(calls.run(long_call, index)) for index in range(2)]
        try:
            for started in entered:
                assert await asyncio.to_thread(started.wait, 1)
            tasks[0].cancel()
            with pytest.raises(BlockingCallCancelled) as caught:
                await tasks[0]
            completion = caught.value.completion
            release[0].set()
            # A sibling model call still owns this execution even after the
            # cancelled call itself exits.
            await asyncio.sleep(0.02)
            assert calls.pending and not completion.done()
            release[1].set()
            await tasks[1]
            await asyncio.wrap_future(completion)
            assert not calls.pending
        finally:
            for finished in release:
                finished.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(execute())
