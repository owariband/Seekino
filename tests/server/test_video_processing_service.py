from __future__ import annotations

import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from contextvars import ContextVar
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from threading import Event, Lock, Thread
from time import sleep
from types import SimpleNamespace

import pytest
from PIL import Image
from engine.parse.schemas import (
    TranscriptAnalysis,
    VideoChunkCheckpoint,
    VideoParseResult,
)
from engine.retrieval.video_retrieval import build_video_chunk_retrieval_documents
from gateway.schemas import EmbeddingResult
from sqlalchemy import select

from server.database import DatabaseEnvironment, create_session_factory
from server.identity import derive_video_chunk_id, derive_video_id
from server.model.dto.jobDTO import ParseJobLease, VideoChunkCheckpointState
from server.model.index_sync import IndexSyncIntentRecord, IndexSyncStatus
from server.model.job import ParseJobStatus, VideoChunkCheckpointStage
from server.model.job_timing import VideoChunkTimingStage
from server.model.video import (
    SeriesRecord,
    VideoChunkEmbeddingRecord,
    VideoChunkRecord,
)
from server.mq.mysql_queue import MySqlParseJobQueue
from server.mq.contracts import ParseJobLeaseLostError
from server.repository.index_sync import IndexSyncRepository
from server.repository.job import ParseJobRepository
from server.repository.library import LibraryRepository
from server.repository.upload import UploadRepository
from server.repository.video import VideoRepository
from server.service.index_sync import IndexSyncService
from server.service.qdrant_mutation_lock import QdrantMutationLock
from server.service.library import LibraryService
from server.service.media import MediaService
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore
from server.service.video_processing import VideoProcessingConfig, VideoProcessingService
from server.utils.time import now_unix_time_ms, timedelta_to_ms
from server.storage.artifact import ArtifactIntegrityError


class _Algorithm:
    def __init__(self, template: VideoParseResult) -> None:
        self.template = template
        self.backgrounds = []
        self.parse_sequences: list[int] = []
        self.transcribe_sequences: list[int] = []
        self.fail_once_at: int | None = None

    def describe_models(self, transcript_mode):
        assert self.template.models is not None
        return self.template.models.model_copy(
            update={
                "transcript": (
                    self.template.models.fallback.transcript_model
                    if transcript_mode == "lite"
                    else None
                ),
                "transcript_mode": transcript_mode,
            }
        )

    def transcribe_chunk(self, video_chunk):
        self.transcribe_sequences.append(video_chunk.sequence_no)
        return TranscriptAnalysis(transcript=[])

    def parse_chunk(
        self,
        compact,
        video_chunk,
        *,
        candidate_transcript=None,
        series_bible,
        background_images,
    ):
        self.backgrounds.append((series_bible, background_images))
        self.parse_sequences.append(video_chunk.sequence_no)
        if self.fail_once_at == video_chunk.sequence_no:
            self.fail_once_at = None
            raise RuntimeError(f"fixture parse failure at {video_chunk.sequence_no}")
        chunk = self.template.chunks[video_chunk.sequence_no]
        return chunk.model_copy(
            update={
                "window": video_chunk,
                "context_before": compact,
                "context_before_compact": compact.compact_text,
                "candidate_transcript": (
                    candidate_transcript.transcript
                    if candidate_transcript is not None
                    else []
                ),
            }
        )


class _Embedder:
    def __init__(self) -> None:
        self.text_inputs: list[str] = []
        self.video_inputs: list[Path] = []

    def embed_text(self, text: str) -> EmbeddingResult:
        self.text_inputs.append(text)
        return EmbeddingResult(model="embedding-test", modality="text", vector=[1, 2, 3])

    def embed_video(self, video_path) -> EmbeddingResult:
        self.video_inputs.append(Path(video_path))
        return EmbeddingResult(model="embedding-test", modality="video", vector=[3, 2, 1])


class _IndexStore:
    def __init__(self) -> None:
        self.points = []
        self.deleted = []

    def upsert(self, point) -> None:
        self.points.append(point)

    def delete(self, video_chunk_id: str) -> None:
        self.deleted.append(video_chunk_id)


class _MemoryJobs:
    def __init__(self) -> None:
        self.current = True
        self.checkpoints = []

    def list_checkpoints(self, _job_id):
        return []

    def assert_current_lease(self, _lease):
        if not self.current:
            raise ParseJobLeaseLostError("fixture lease lost")

    def update_progress(self, _lease, *, stage):
        del stage
        return self.current

    def save_checkpoint(self, _lease, checkpoint):
        self.assert_current_lease(_lease)
        self.checkpoints.append(checkpoint)
        return checkpoint


class _ConcurrentMemoryJobs:
    def __init__(self) -> None:
        self.current_version = 1
        self.by_sequence = {}
        self.lock = Lock()

    def list_checkpoints(self, _job_id):
        with self.lock:
            return [self.by_sequence[key] for key in sorted(self.by_sequence)]

    def assert_current_lease(self, lease):
        with self.lock:
            if lease.lease_version != self.current_version:
                raise ParseJobLeaseLostError("fixture lease lost")

    def update_progress(self, lease, *, stage):
        del stage
        with self.lock:
            return lease.lease_version == self.current_version

    def save_checkpoint(self, lease, checkpoint):
        self.assert_current_lease(lease)
        with self.lock:
            current = self.by_sequence.get(checkpoint.sequence_no)
            if current is not None:
                checkpoint = replace(
                    checkpoint,
                    chunk_start_ms=(
                        checkpoint.chunk_start_ms
                        if checkpoint.chunk_start_ms is not None
                        else current.chunk_start_ms
                    ),
                    chunk_end_ms=(
                        checkpoint.chunk_end_ms
                        if checkpoint.chunk_end_ms is not None
                        else current.chunk_end_ms
                    ),
                    chunk_media_uri=(
                        checkpoint.chunk_media_uri or current.chunk_media_uri
                    ),
                    analysis_window_media_uri=(
                        checkpoint.analysis_window_media_uri
                        or current.analysis_window_media_uri
                    ),
                    parse_artifact_uri=(
                        checkpoint.parse_artifact_uri or current.parse_artifact_uri
                    ),
                    text_embedding_artifact_uri=(
                        checkpoint.text_embedding_artifact_uri
                        or current.text_embedding_artifact_uri
                    ),
                    vision_embedding_artifact_uri=(
                        checkpoint.vision_embedding_artifact_uri
                        or current.vision_embedding_artifact_uri
                    ),
                )
            self.by_sequence[checkpoint.sequence_no] = checkpoint
            return checkpoint

    def move_to_lease(self, lease_version):
        with self.lock:
            self.current_version = lease_version


class _MemoryArtifacts:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths = {}

    def work_directory(self, job_id):
        path = self.root / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def publish_video_artifact(self, source_path, **kwargs):
        uri = (
            f"artifact://test/published/{kwargs['category']}/"
            f"{kwargs['filename']}"
        )
        self.paths[uri] = Path(source_path)
        return SimpleNamespace(uri=uri)

    def uri_for_path(self, path):
        resolved = Path(path).resolve()
        relative = resolved.relative_to(self.root.resolve())
        uri = f"artifact://test/{relative.as_posix()}"
        self.paths[uri] = resolved
        return uri

    def resolve(self, uri):
        try:
            return self.paths[uri]
        except KeyError as exc:
            raise FileNotFoundError(uri) from exc

    def fingerprint(self, uri):
        path = self.resolve(uri)
        return SimpleNamespace(sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _lease() -> ParseJobLease:
    return ParseJobLease(
        job_id="11111111-1111-4111-8111-111111111111",
        series_id="22222222-2222-4222-8222-222222222222",
        video_id="33333333-3333-4333-8333-333333333333",
        pipeline_version="pipeline-v1",
        worker_id="consumer-test",
        lease_version=1,
        lease_expires_at=now_unix_time_ms() + timedelta_to_ms(timedelta(minutes=5)),
        attempt_count=1,
        max_attempts=3,
        episode_order=1,
        upload_sequence=1,
        original_filename="episode.mp4",
        original_source_uri="artifact://test/original.mp4",
        content_hash="0" * 64,
        total_bytes=1,
        duration_ms=20_000,
        workspace_id="local-default",
        series_name="Fixture",
        series_description=None,
        important_extra_data=None,
        character_bible=[],
    )


def _materialize_analysis(windows, **_kwargs):
    for window in windows:
        window.media_path.parent.mkdir(parents=True, exist_ok=True)
        window.media_path.write_bytes(f"analysis-{window.sequence_no}".encode())


def _materialize_canonical(windows, *, output_dir, **_kwargs):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for window in windows:
        path = directory / f"chunk-{window.sequence_no:04d}.mp4"
        path.write_bytes(f"canonical-{window.sequence_no}".encode())
        paths.append(path)
    return paths


def _materialize_analysis_one(window, **kwargs):
    _materialize_analysis([window], **kwargs)


def _materialize_canonical_one(window, *, output_dir, **kwargs):
    return _materialize_canonical([window], output_dir=output_dir, **kwargs)[0]


def _extract_frames(video_path, timestamps_ms, *, output_dir, **_kwargs):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, timestamp_ms in enumerate(timestamps_ms):
        path = directory / f"frame-{index:03d}-{timestamp_ms:08d}ms.jpg"
        path.write_bytes(b"\xff\xd8\xff" + Path(video_path).read_bytes())
        frames.append((timestamp_ms, path))
    return frames


class _VideoSink:
    def __init__(self) -> None:
        self.called = False

    def import_video_parse_for_job(self, *_args, **_kwargs):
        self.called = True
        raise AssertionError("video import should not be reached")


class _IndexSink:
    def __init__(self) -> None:
        self.called = False

    def sync_video_chunks(self, _video_chunk_ids):
        self.called = True
        raise AssertionError("index sync should not be reached")


class _SuccessfulVideoSink:
    def __init__(self, segment_count: int) -> None:
        self._segment_count = segment_count

    def import_video_parse_for_job(self, _prepared, lease):
        return SimpleNamespace(video_chunk_ids=[
            derive_video_chunk_id(lease.video_id, sequence_no)
            for sequence_no in range(self._segment_count)
        ])


class _SuccessfulIndexSink:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def sync_video_chunks(self, video_chunk_ids):
        self.calls.append(list(video_chunk_ids))


class _RecordingStageTimer:
    def __init__(self) -> None:
        self.calls: list[tuple[int, VideoChunkTimingStage]] = []

    @contextmanager
    def measure(self, _lease, *, sequence_no, stage):
        self.calls.append((sequence_no, stage))
        yield


def _unit_processor(
    tmp_path,
    monkeypatch,
    algorithm,
    jobs,
    *,
    asr_mode,
    chunk_duration_ms=10_000,
    stage_timer=None,
    videos=None,
    index=None,
):
    artifacts = _MemoryArtifacts(tmp_path / "artifacts")
    videos = videos or _VideoSink()
    index = index or _IndexSink()
    processor = VideoProcessingService(
        jobs,  # type: ignore[arg-type]
        videos,  # type: ignore[arg-type]
        index,  # type: ignore[arg-type]
        artifacts,  # type: ignore[arg-type]
        algorithm,
        _Embedder(),
        config=VideoProcessingConfig(
            embedding_dimensions=3,
            chunk_duration_ms=chunk_duration_ms,
            context_overlap_ms=0,
            asr_concurrency=2,
            asr_mode=asr_mode,
        ),
        stage_timer=stage_timer,
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(processor, "_verified_source", lambda _lease: source)
    monkeypatch.setattr(
        processor,
        "_parse_background",
        lambda *_args: (None, [], False),
    )
    monkeypatch.setattr(
        "server.service.video_processing.materialize_analysis_window",
        _materialize_analysis_one,
    )
    monkeypatch.setattr(
        "server.service.video_processing.materialize_canonical_chunk",
        _materialize_canonical_one,
    )
    monkeypatch.setattr(
        "server.service.video_processing.extract_video_frames",
        _extract_frames,
    )
    return processor, artifacts, videos, index


def test_processing_service_measures_every_stage_for_each_segment(
    prepared_import,
    tmp_path,
    monkeypatch,
) -> None:
    algorithm = _Algorithm(prepared_import.parse_result)
    segment_count = len(prepared_import.parse_result.chunks)
    timer = _RecordingStageTimer()
    index = _SuccessfulIndexSink()
    lease = _lease()
    lease = replace(
        lease,
        video_id=derive_video_id(lease.series_id, lease.content_hash),
    )
    processor, _artifacts, _videos, _index = _unit_processor(
        tmp_path,
        monkeypatch,
        algorithm,
        _MemoryJobs(),
        asr_mode="lite",
        stage_timer=timer,
        videos=_SuccessfulVideoSink(segment_count),
        index=index,
    )

    assert len(processor.process(lease)) == segment_count
    stages = (
        VideoChunkTimingStage.SPLIT,
        VideoChunkTimingStage.ASR,
        VideoChunkTimingStage.PARSE,
        VideoChunkTimingStage.TEXT_EMBEDDING,
        VideoChunkTimingStage.VISION_EMBEDDING,
        VideoChunkTimingStage.INDEX,
    )
    assert Counter(timer.calls) == Counter(
        (sequence_no, stage)
        for sequence_no in range(segment_count)
        for stage in stages
    )
    assert len(index.calls) == segment_count


def test_processing_service_ignores_stray_json_restores_db_prefix_and_reaches_ready(
    repository,
    prepared_import,
    tmp_path,
    monkeypatch,
) -> None:
    engine, _existing_video_repository = repository
    session_factory = create_session_factory(engine)
    artifacts = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    library = LibraryService(LibraryRepository(session_factory))
    series = library.create_series(
        workspace_id="local-default",
        name="Worker Processing Series",
        description="fixture",
        character_bible=[{"name": "Anon"}],
    )
    image_bytes = BytesIO()
    Image.new("RGB", (2048, 1024), "blue").save(image_bytes, format="PNG")
    image = MediaService(library, artifacts).upload_series_image(
        series.series_id, image_bytes.getvalue(), workspace_id="local-default",
    )
    background = f"# Anon\n![角色参考](asset://{image.image_id})\n蓝发角色"
    with session_factory.begin() as session:
        session.get(SeriesRecord, series.series_id).important_extra_data = {
            "format": "markdown", "content": background,
        }
    source_bytes = b"not-a-real-video-but-content-addressed"
    upload = UploadService(
        UploadRepository(session_factory),
        artifacts,
        config=UploadServiceConfig(
            preferred_segment_size_bytes=1024,
            maximum_upload_size_bytes=4096,
            pipeline_version="pipeline-v1",
        ),
        probe_duration=lambda _path: prepared_import.parse_result.duration_ms,
    )
    upload_session = upload.create_session(
        workspace_id="local-default",
        series_id=series.series_id,
        episode_order=1,
        original_filename="episode.mp4",
        media_type="video/mp4",
        total_size_bytes=len(source_bytes),
        idempotency_key="processing-fixture",
    )
    upload.upload_segment(
        upload_session.upload_session_id,
        0,
        source_bytes,
        workspace_id="local-default",
        checksum_sha256=hashlib.sha256(source_bytes).hexdigest(),
        start_byte=0,
        end_byte_exclusive=len(source_bytes),
        total_size_bytes=len(source_bytes),
    )
    completed = upload.complete(
        upload_session.upload_session_id,
        workspace_id="local-default",
    )
    jobs = ParseJobRepository(session_factory)
    queue = MySqlParseJobQueue(session_factory)
    lease = queue.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=5),
    )
    assert lease is not None
    assert lease.job_id == completed.job_id

    def materialize_analysis(windows, **_kwargs):
        for window in windows:
            window.media_path.parent.mkdir(parents=True, exist_ok=True)
            window.media_path.write_bytes(
                f"analysis-{window.sequence_no}".encode()
            )

    def materialize_canonical(windows, *, output_dir, **_kwargs):
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for window in windows:
            path = directory / f"chunk-{window.sequence_no:04d}.mp4"
            path.write_bytes(f"canonical-{window.sequence_no}".encode())
            paths.append(path)
        return paths

    def materialize_analysis_one(window, **kwargs):
        materialize_analysis([window], **kwargs)

    def materialize_canonical_one(window, *, output_dir, **kwargs):
        return materialize_canonical([window], output_dir=output_dir, **kwargs)[0]

    monkeypatch.setattr(
        "server.service.video_processing.materialize_analysis_window",
        materialize_analysis_one,
    )
    monkeypatch.setattr(
        "server.service.video_processing.materialize_canonical_chunk",
        materialize_canonical_one,
    )
    monkeypatch.setattr(
        "server.service.video_processing.extract_video_frames",
        _extract_frames,
    )
    embedder = _Embedder()
    index_store = _IndexStore()
    algorithm = _Algorithm(prepared_import.parse_result)
    index_sync = IndexSyncService(
        IndexSyncRepository(session_factory),
        index_store,  # type: ignore[arg-type]
        mutation_lock=QdrantMutationLock(
            engine,
            environment=DatabaseEnvironment.TEST,
            collection_name="video_chunks_test",
        ),
    )
    sync_calls: list[list[str]] = []
    original_sync = index_sync.sync_video_chunks

    def record_sync(video_chunk_ids):
        sync_calls.append(list(video_chunk_ids))
        return original_sync(video_chunk_ids)

    monkeypatch.setattr(index_sync, "sync_video_chunks", record_sync)
    processor = VideoProcessingService(
        jobs,
        VideoRepository(session_factory),
        index_sync,
        artifacts,
        algorithm,
        embedder,
        config=VideoProcessingConfig(
            embedding_dimensions=3,
            chunk_duration_ms=10_000,
            context_overlap_ms=0,
            asr_mode="none",
        ),
    )

    stray_path = artifacts.work_directory(lease.job_id) / "chunks" / "chunk-0000.json"
    stray_path.parent.mkdir(parents=True, exist_ok=True)
    stray_path.write_text('{"stray": true}', encoding="utf-8")
    first_document = build_video_chunk_retrieval_documents(
        prepared_import.parse_result
    )[0]
    stray_embedding = (
        artifacts.work_directory(lease.job_id)
        / "leases"
        / f"lease-{lease.lease_version:08d}"
        / "embeddings"
        / "chunk-0000.text.json"
    )
    stray_embedding.parent.mkdir(parents=True, exist_ok=True)
    stray_embedding.write_text(
        json.dumps(
            {
                "video_chunk_id": derive_video_chunk_id(lease.video_id, 0),
                "input_sha256": hashlib.sha256(
                    first_document.text.encode("utf-8")
                ).hexdigest(),
                "embedding": EmbeddingResult(
                    model="stale-embedding",
                    modality="text",
                    vector=[9, 9, 9],
                ).model_dump(mode="json", exclude_computed_fields=True),
            }
        ),
        encoding="utf-8",
    )
    algorithm.fail_once_at = 1
    with pytest.raises(RuntimeError, match="fixture parse failure at 1"):
        processor.process(lease)
    partial_checkpoints = jobs.list_checkpoints(completed.job_id)
    assert [item.stage for item in partial_checkpoints] == [
        VideoChunkCheckpointStage.PARSED.value,
        VideoChunkCheckpointStage.SPLIT_READY.value,
    ]
    assert partial_checkpoints[0].parse_artifact_uri is not None
    assert partial_checkpoints[1].parse_artifact_uri is None

    video_chunk_ids = processor.process(lease)
    assert jobs.get(completed.job_id).progress == 99
    edited_lease = replace(lease, important_extra_data={"format": "markdown", "content": "新背景"})
    first_checkpoint = jobs.list_checkpoints(completed.job_id)[0]
    assert first_checkpoint.text_embedding_artifact_uri is not None
    committed_embedding = artifacts.resolve(
        first_checkpoint.text_embedding_artifact_uri
    )
    saved_embedding = committed_embedding.read_bytes()
    committed_embedding.write_text('{"corrupted": true}', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="committed text embedding"):
        processor.process(edited_lease)
    committed_embedding.write_bytes(saved_embedding)
    repeated_video_chunk_ids = processor.process(edited_lease)

    assert repeated_video_chunk_ids == video_chunk_ids
    assert len(video_chunk_ids) == 2
    assert len(embedder.text_inputs) == 2
    assert all("第" in text for text in embedder.text_inputs)
    assert [
        path.name.endswith(f"-chunk-{sequence_no:04d}.mp4")
        for sequence_no, path in enumerate(embedder.video_inputs)
    ] == [True, True]
    assert all(lease.job_id in path.name for path in embedder.video_inputs)
    assert len(index_store.points) == 2
    assert sync_calls[:2] == [[video_chunk_ids[0]], [video_chunk_ids[1]]]
    assert algorithm.parse_sequences == [0, 1, 1]
    assert algorithm.transcribe_sequences == []
    assert len(algorithm.backgrounds) == 3
    text, references = algorithm.backgrounds[0]
    assert all(item == algorithm.backgrounds[0] for item in algorithm.backgrounds)
    assert background in text
    assert "Anon" in text
    assert [reference.image_id for reference in references] == [image.image_id]
    with Image.open(references[0].path) as model_image:
        assert model_image.size == (1024, 512)
    snapshot_path = artifacts.work_directory(lease.job_id) / "background.snapshot.json"
    saved_snapshot = snapshot_path.read_bytes()
    references[0].path.write_bytes(b"changed-image")
    with pytest.raises(ArtifactIntegrityError, match="内容已改变"):
        processor._parse_background(edited_lease, snapshot_path.parent, {})
    assert snapshot_path.read_bytes() == saved_snapshot
    # A completed legacy parse can finish persistence without adopting new background.
    snapshot_path.unlink()
    assert processor.process(edited_lease) == video_chunk_ids
    assert algorithm.parse_sequences == [0, 1, 1]
    assert not snapshot_path.exists()
    assert queue.acknowledge(lease, duration_ms=0)
    checkpoints = jobs.list_checkpoints(completed.job_id)
    assert {item.stage for item in checkpoints} == {
        VideoChunkCheckpointStage.INDEXED.value
    }
    assert [
        (item.chunk_start_ms, item.chunk_end_ms) for item in checkpoints
    ] == [(0, 10_000), (10_000, 20_000)]
    published_identity = f"{lease.job_id}-lease-{lease.lease_version:08d}"
    assert all(
        published_identity in item.chunk_media_uri
        and published_identity in item.analysis_window_media_uri
        for item in checkpoints
    )
    assert jobs.get(completed.job_id).progress == 100
    with session_factory() as session:
        series_record = session.get(SeriesRecord, series.series_id)
        chunks = session.scalars(select(VideoChunkRecord)).all()
        embeddings = session.scalars(select(VideoChunkEmbeddingRecord)).all()
        intents = session.scalars(select(IndexSyncIntentRecord)).all()
    assert series_record is not None
    assert series_record.status == "READY"
    video = library.get_video(
        completed.video_id,
        workspace_id="local-default",
    )
    assert video.parse_status == ParseJobStatus.SUCCEEDED.value
    assert len(chunks) == len(embeddings) == len(intents) == 2
    assert [item.keyframe_uris[0]["at_ms"] for item in chunks] == [0, 10_000]
    assert all(
        "/keyframes/" in item.keyframe_uris[0]["uri"]
        for item in chunks
    )
    assert all(item.text_vector == [1.0, 2.0, 3.0] for item in embeddings)
    assert all(item.vision_vector == [3.0, 2.0, 1.0] for item in embeddings)
    assert all(item.status == IndexSyncStatus.SYNCED.value for item in intents)


def test_processing_service_runs_asr_ahead_but_parses_in_sequence(
    prepared_import,
    tmp_path,
    monkeypatch,
):
    asr_context = ContextVar("asr_context", default="missing")

    class ConcurrentAlgorithm(_Algorithm):
        def __init__(self, template):
            super().__init__(template)
            self.lock = Lock()
            self.second_started = Event()
            self.active = 0
            self.maximum_active = 0
            self.finish_order = []
            self.context_values = []

        def transcribe_chunk(self, video_chunk):
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                self.context_values.append(asr_context.get())
            try:
                if video_chunk.sequence_no == 0:
                    assert self.second_started.wait(timeout=2)
                    sleep(0.05)
                else:
                    self.second_started.set()
                with self.lock:
                    self.finish_order.append(video_chunk.sequence_no)
                return TranscriptAnalysis(transcript=[])
            finally:
                with self.lock:
                    self.active -= 1

    class ParsingReached(RuntimeError):
        pass

    jobs = _MemoryJobs()
    algorithm = ConcurrentAlgorithm(prepared_import.parse_result)
    processor, artifacts, _videos, _index = _unit_processor(
        tmp_path,
        monkeypatch,
        algorithm,
        jobs,
        asr_mode="lite",
    )

    def stop_after_parse(*_args, **_kwargs):
        raise ParsingReached

    monkeypatch.setattr(processor, "_embed_text_chunks", stop_after_parse)
    token = asr_context.set("parse-usage-capture")
    try:
        with pytest.raises(ParsingReached):
            processor.process(_lease())
    finally:
        asr_context.reset(token)

    assert algorithm.maximum_active == 2
    assert algorithm.finish_order == [1, 0]
    assert algorithm.parse_sequences == [0, 1]
    assert algorithm.context_values == ["parse-usage-capture"] * 2
    assert (
        artifacts.work_directory(_lease().job_id)
        / "leases"
        / "lease-00000001"
        / "pipeline.parse.json"
    ).is_file()


def test_asr_workers_refill_while_an_earlier_sequence_is_still_running(
    prepared_import,
    tmp_path,
    monkeypatch,
):
    release_first = Event()
    third_started = Event()
    fourth_started = Event()

    class RefillAlgorithm(_Algorithm):
        def transcribe_chunk(self, video_chunk):
            if video_chunk.sequence_no == 0:
                assert release_first.wait(timeout=5)
            elif video_chunk.sequence_no == 2:
                third_started.set()
            elif video_chunk.sequence_no == 3:
                fourth_started.set()
            return TranscriptAnalysis(transcript=[])

    runtime = RefillAlgorithm(prepared_import.parse_result)
    processor, _artifacts, _videos, _index = _unit_processor(
        tmp_path,
        monkeypatch,
        runtime,
        _MemoryJobs(),
        asr_mode="lite",
    )
    template = prepared_import.parse_result.chunks[0].window
    windows = [
        template.model_copy(update={"sequence_no": sequence_no})
        for sequence_no in range(4)
    ]
    consumed = []
    errors = []

    def consume():
        try:
            consumed.extend(
                window.sequence_no
                for window, _transcript in processor._ordered_candidate_transcripts(
                    _lease(),
                    windows,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    consumer = Thread(target=consume)
    consumer.start()
    refilled = third_started.wait(timeout=5)
    overfilled = fourth_started.wait(timeout=0.1)
    release_first.set()
    consumer.join(timeout=5)

    assert refilled
    assert not overfilled
    assert not consumer.is_alive()
    assert errors == []
    assert consumed == [0, 1, 2, 3]


@pytest.mark.parametrize("failure", ["parse", "lease"])
def test_asr_workers_stop_safely_after_processing_failure(
    failure,
    prepared_import,
    tmp_path,
    monkeypatch,
):
    jobs = _MemoryJobs()

    class FailingRuntime(_Algorithm):
        def __init__(self, template):
            super().__init__(template)
            self.calls = []
            self.active = 0
            self.lock = Lock()

        def transcribe_chunk(self, video_chunk):
            with self.lock:
                self.calls.append(video_chunk.sequence_no)
                self.active += 1
            try:
                sleep(0.01)
                if failure == "lease" and video_chunk.sequence_no == 0:
                    jobs.current = False
                return TranscriptAnalysis(transcript=[])
            finally:
                with self.lock:
                    self.active -= 1

        def parse_chunk(self, *args, **kwargs):
            if failure == "parse":
                raise RuntimeError("fixture parse stopped")
            return super().parse_chunk(*args, **kwargs)

    runtime = FailingRuntime(prepared_import.parse_result)
    processor, _artifacts, _videos, _index = _unit_processor(
        tmp_path,
        monkeypatch,
        runtime,
        jobs,
        asr_mode="lite",
        chunk_duration_ms=2_000,
    )

    expected_error = RuntimeError if failure == "parse" else ParseJobLeaseLostError
    with pytest.raises(expected_error):
        processor.process(_lease())

    calls_after_exit = list(runtime.calls)
    sleep(0.05)
    assert runtime.active == 0
    assert runtime.calls == calls_after_exit
    assert len(runtime.calls) <= 4


def test_parse_result_is_discarded_when_lease_is_lost_before_persistence(
    prepared_import,
    tmp_path,
    monkeypatch,
):
    jobs = _MemoryJobs()

    class LeaseLosingAlgorithm(_Algorithm):
        def parse_chunk(self, *args, **kwargs):
            chunk = super().parse_chunk(*args, **kwargs)
            jobs.current = False
            return chunk

    algorithm = LeaseLosingAlgorithm(prepared_import.parse_result)
    processor, artifacts, videos, index = _unit_processor(
        tmp_path,
        monkeypatch,
        algorithm,
        jobs,
        asr_mode="none",
    )
    embedder = processor._embedder

    with pytest.raises(ParseJobLeaseLostError, match="fixture lease lost"):
        processor.process(_lease())

    work_directory = artifacts.work_directory(_lease().job_id)
    attempt_directory = work_directory / "leases" / "lease-00000001"
    assert not (attempt_directory / "chunks").exists()
    assert not (attempt_directory / "pipeline.parse.json").exists()
    assert all(
        checkpoint.stage != VideoChunkCheckpointStage.PARSED.value
        for checkpoint in jobs.checkpoints
    )
    assert embedder.text_inputs == []
    assert embedder.video_inputs == []
    assert not videos.called
    assert not index.called


def test_late_old_lease_write_cannot_replace_new_lease_parse_artifact(
    prepared_import,
    tmp_path,
    monkeypatch,
):
    class NamedAlgorithm(_Algorithm):
        def __init__(self, template, name):
            super().__init__(template)
            self.name = name

        def parse_chunk(self, *args, **kwargs):
            return super().parse_chunk(*args, **kwargs).model_copy(
                update={"window_summary": self.name}
            )

    class ParsingReached(RuntimeError):
        pass

    jobs = _ConcurrentMemoryJobs()
    old_runtime = NamedAlgorithm(prepared_import.parse_result, "old lease")
    new_runtime = NamedAlgorithm(prepared_import.parse_result, "new lease")
    old_processor, old_artifacts, _old_videos, _old_index = _unit_processor(
        tmp_path,
        monkeypatch,
        old_runtime,
        jobs,
        asr_mode="none",
    )
    new_processor, new_artifacts, _new_videos, _new_index = _unit_processor(
        tmp_path,
        monkeypatch,
        new_runtime,
        jobs,
        asr_mode="none",
    )

    def stop_after_parse(*_args, **_kwargs):
        raise ParsingReached

    monkeypatch.setattr(old_processor, "_embed_text_chunks", stop_after_parse)
    monkeypatch.setattr(new_processor, "_embed_text_chunks", stop_after_parse)

    from server.service import video_processing as processing_module

    original_write = processing_module._write_json_atomic
    old_write_started = Event()
    allow_old_write = Event()

    def block_old_parse_write(path, payload):
        if (
            "lease-00000001" in path.parts
            and path.parent.name == "chunks"
        ):
            old_write_started.set()
            assert allow_old_write.wait(timeout=5)
        original_write(path, payload)

    monkeypatch.setattr(
        processing_module,
        "_write_json_atomic",
        block_old_parse_write,
    )
    first_lease = _lease()
    second_lease = replace(
        first_lease,
        lease_version=2,
        worker_id="consumer-new",
    )
    old_errors = []

    def run_old_lease():
        try:
            old_processor.process(first_lease)
        except BaseException as exc:
            old_errors.append(exc)

    old_thread = Thread(target=run_old_lease)
    old_thread.start()
    assert old_write_started.wait(timeout=5)

    jobs.move_to_lease(second_lease.lease_version)
    with pytest.raises(ParsingReached):
        new_processor.process(second_lease)
    committed = jobs.by_sequence[0]
    assert committed.parse_artifact_uri is not None
    assert "lease-00000002" in committed.parse_artifact_uri

    allow_old_write.set()
    old_thread.join(timeout=5)
    assert not old_thread.is_alive()
    assert len(old_errors) == 1
    assert isinstance(old_errors[0], ParseJobLeaseLostError)

    committed_path = new_artifacts.resolve(committed.parse_artifact_uri)
    committed_artifact = VideoChunkCheckpoint.model_validate_json(
        committed_path.read_text(encoding="utf-8")
    )
    assert committed_artifact.chunk.window_summary == "new lease"
    old_path = (
        old_artifacts.work_directory(first_lease.job_id)
        / "leases"
        / "lease-00000001"
        / "chunks"
        / "chunk-0000.json"
    )
    old_artifact = VideoChunkCheckpoint.model_validate_json(
        old_path.read_text(encoding="utf-8")
    )
    assert old_artifact.chunk.window_summary == "old lease"
    assert committed_path != old_path


def test_late_old_lease_media_materialization_cannot_pollute_new_media_pointer(
    prepared_import,
    tmp_path,
    monkeypatch,
):
    class ParsingReached(RuntimeError):
        pass

    jobs = _ConcurrentMemoryJobs()
    old_processor, old_artifacts, _old_videos, _old_index = _unit_processor(
        tmp_path,
        monkeypatch,
        _Algorithm(prepared_import.parse_result),
        jobs,
        asr_mode="none",
    )
    new_processor, new_artifacts, _new_videos, _new_index = _unit_processor(
        tmp_path,
        monkeypatch,
        _Algorithm(prepared_import.parse_result),
        jobs,
        asr_mode="none",
    )

    def stop_after_parse(*_args, **_kwargs):
        raise ParsingReached

    monkeypatch.setattr(old_processor, "_embed_text_chunks", stop_after_parse)
    monkeypatch.setattr(new_processor, "_embed_text_chunks", stop_after_parse)
    old_materialization_started = Event()
    allow_old_materialization = Event()

    def materialize_analysis(windows, **_kwargs):
        old_attempt = "lease-00000001" in windows[0].media_path.parts
        if old_attempt:
            old_materialization_started.set()
            assert allow_old_materialization.wait(timeout=5)
        label = "old" if old_attempt else "new"
        for window in windows:
            window.media_path.parent.mkdir(parents=True, exist_ok=True)
            window.media_path.write_bytes(
                f"{label}-analysis-{window.sequence_no}".encode()
            )

    def materialize_canonical(windows, *, output_dir, **_kwargs):
        old_attempt = "lease-00000001" in Path(output_dir).parts
        label = "old" if old_attempt else "new"
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for window in windows:
            path = directory / f"chunk-{window.sequence_no:04d}.mp4"
            path.write_bytes(f"{label}-canonical-{window.sequence_no}".encode())
            paths.append(path)
        return paths

    def materialize_analysis_one(window, **kwargs):
        materialize_analysis([window], **kwargs)

    def materialize_canonical_one(window, *, output_dir, **kwargs):
        return materialize_canonical([window], output_dir=output_dir, **kwargs)[0]

    monkeypatch.setattr(
        "server.service.video_processing.materialize_analysis_window",
        materialize_analysis_one,
    )
    monkeypatch.setattr(
        "server.service.video_processing.materialize_canonical_chunk",
        materialize_canonical_one,
    )
    monkeypatch.setattr(
        "server.service.video_processing.extract_video_frames",
        _extract_frames,
    )
    first_lease = _lease()
    second_lease = replace(
        first_lease,
        lease_version=2,
        worker_id="consumer-new",
    )
    old_errors = []

    def run_old_lease():
        try:
            old_processor.process(first_lease)
        except BaseException as exc:
            old_errors.append(exc)

    old_thread = Thread(target=run_old_lease)
    old_thread.start()
    assert old_materialization_started.wait(timeout=5)
    jobs.move_to_lease(second_lease.lease_version)

    with pytest.raises(ParsingReached):
        new_processor.process(second_lease)
    committed = jobs.by_sequence[0]
    assert committed.chunk_media_uri is not None
    assert committed.analysis_window_media_uri is not None
    expected_identity = (
        f"{second_lease.job_id}-lease-{second_lease.lease_version:08d}"
    )
    assert expected_identity in committed.chunk_media_uri
    assert expected_identity in committed.analysis_window_media_uri

    allow_old_materialization.set()
    old_thread.join(timeout=5)
    assert not old_thread.is_alive()
    assert len(old_errors) == 1
    assert isinstance(old_errors[0], ParseJobLeaseLostError)

    assert new_artifacts.resolve(committed.chunk_media_uri).read_bytes() == (
        b"new-canonical-0"
    )
    assert new_artifacts.resolve(
        committed.analysis_window_media_uri
    ).read_bytes() == b"new-analysis-0"
    old_attempt = (
        old_artifacts.work_directory(first_lease.job_id)
        / "leases"
        / "lease-00000001"
    )
    assert (old_attempt / "canonical-chunks" / "chunk-0000.mp4").read_bytes() == (
        b"old-canonical-0"
    )
    old_analysis = next((old_attempt / "windows").glob("chunk-0000.*.mp4"))
    assert old_analysis.read_bytes() == b"old-analysis-0"


@pytest.mark.parametrize("inconsistent", ["missing_pointer", "gap", "extra"])
def test_db_parse_checkpoints_must_be_a_complete_continuous_prefix(
    inconsistent,
    prepared_import,
    tmp_path,
    monkeypatch,
):
    jobs = _MemoryJobs()
    algorithm = _Algorithm(prepared_import.parse_result)
    processor, _artifacts, _videos, _index = _unit_processor(
        tmp_path,
        monkeypatch,
        algorithm,
        jobs,
        asr_mode="none",
    )
    windows = [chunk.window for chunk in prepared_import.parse_result.chunks]

    def checkpoint(sequence_no, stage, uri=None):
        return VideoChunkCheckpointState(
            job_id=_lease().job_id,
            sequence_no=sequence_no,
            video_chunk_id=f"chunk-{sequence_no}",
            stage=stage,
            parse_artifact_uri=uri,
        )

    if inconsistent == "missing_pointer":
        checkpoints = {
            0: checkpoint(0, VideoChunkCheckpointStage.PARSED.value),
            1: checkpoint(1, VideoChunkCheckpointStage.SPLIT_READY.value),
        }
    elif inconsistent == "gap":
        checkpoints = {
            0: checkpoint(0, VideoChunkCheckpointStage.SPLIT_READY.value),
            1: checkpoint(
                1,
                VideoChunkCheckpointStage.PARSED.value,
                "artifact://test/work/chunk-0001.json",
            ),
        }
    else:
        checkpoints = {
            0: checkpoint(0, VideoChunkCheckpointStage.SPLIT_READY.value),
            1: checkpoint(1, VideoChunkCheckpointStage.SPLIT_READY.value),
            2: checkpoint(2, VideoChunkCheckpointStage.SPLIT_READY.value),
        }

    with pytest.raises(ArtifactIntegrityError):
        processor._load_completed_prefix(
            windows,
            checkpoints,
            models=algorithm.describe_models("none"),
        )


def test_background_snapshot_publish_keeps_first_complete_version(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from server.utils.files import write_json_once

    target = tmp_path / "background.snapshot.json"
    candidates = [{"content": f"背景 {index}", "images": list(range(100))} for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda data: write_json_once(target, data), candidates))
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved in candidates
    write_json_once(target, {"content": "later"})
    assert json.loads(target.read_text(encoding="utf-8")) == saved
    assert list(tmp_path.iterdir()) == [target]
