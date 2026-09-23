from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest
from engine.parse.embed import ChunkEmbedding
from engine.parse.pipeline import ParsePipelineResult
from engine.parse.schemas import VideoChunkCheckpoint
from engine.retrieval.video_retrieval import (
    build_video_chunk_retrieval_documents,
)
from gateway.embedding import EmbeddingResult

from server.config.video_processing import VideoProcessingConfig
from server.identity import derive_video_chunk_id
from server.model.dto.jobDTO import ParseJobLease, VideoChunkCheckpointState
from server.model.job import ParseJobStage, VideoChunkCheckpointStage
from server.mq.contracts import ParseJobLeaseLostError
from server.schema import VideoChunkKeyframeReference
from server.service.parse_persistence import VideoParsePersistence
from server.storage.artifact import ArtifactIntegrityError
from server.utils.time import now_unix_time_ms, timedelta_to_ms


class _MemoryJobs:
    def __init__(
        self,
        checkpoints: dict[int, VideoChunkCheckpointState],
    ) -> None:
        self.current_version = 1
        self.by_sequence = dict(checkpoints)
        self.processing_durations: list[int] = []
        self.progress_stages: list[ParseJobStage] = []
        self.lock = Lock()

    def assert_current_lease(self, lease):
        with self.lock:
            if lease.lease_version != self.current_version:
                raise ParseJobLeaseLostError("fixture lease lost")

    def update_progress(self, lease, *, stage):
        with self.lock:
            if lease.lease_version != self.current_version:
                return False
            self.progress_stages.append(ParseJobStage(stage))
            return True

    def save_checkpoint(
        self,
        lease,
        checkpoint,
        *,
        processing_duration_ms=None,
    ):
        self.assert_current_lease(lease)
        with self.lock:
            current = self.by_sequence.get(checkpoint.sequence_no)
            if current is not None:
                checkpoint = _merge_checkpoint(current, checkpoint)
            self.by_sequence[checkpoint.sequence_no] = checkpoint
            if processing_duration_ms is not None:
                self.processing_durations.append(processing_duration_ms)
            return checkpoint

    def move_to_lease(self, lease_version: int) -> None:
        with self.lock:
            self.current_version = lease_version


def _merge_checkpoint(current, checkpoint):
    return replace(
        checkpoint,
        chunk_start_seconds=(
            checkpoint.chunk_start_seconds
            if checkpoint.chunk_start_seconds is not None
            else current.chunk_start_seconds
        ),
        chunk_end_seconds=(
            checkpoint.chunk_end_seconds
            if checkpoint.chunk_end_seconds is not None
            else current.chunk_end_seconds
        ),
        chunk_media_uri=checkpoint.chunk_media_uri or current.chunk_media_uri,
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


class _MemoryArtifacts:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.paths: dict[str, Path] = {}

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


class _VideoSink:
    def __init__(self) -> None:
        self.prepared = None
        self.lease = None

    def import_video_parse_for_job(self, prepared, lease):
        self.prepared = prepared
        self.lease = lease
        return SimpleNamespace(
            video_chunk_ids=[item.video_chunk_id for item in prepared.chunks]
        )


def _lease(prepared_import, *, lease_version: int = 1) -> ParseJobLease:
    return ParseJobLease(
        job_id="11111111-1111-4111-8111-111111111111",
        series_id=prepared_import.series_id,
        video_id=prepared_import.video_id,
        pipeline_version=prepared_import.pipeline_version,
        worker_id=f"consumer-{lease_version}",
        lease_version=lease_version,
        lease_expires_at=now_unix_time_ms()
        + timedelta_to_ms(timedelta(minutes=5)),
        attempt_count=lease_version,
        max_attempts=3,
        processing_duration_base_ms=400,
        episode_order=prepared_import.episode_order,
        upload_sequence=prepared_import.upload_sequence,
        original_filename=prepared_import.original_filename,
        original_source_uri=prepared_import.original_source_uri,
        content_hash=prepared_import.content_hash,
        total_bytes=prepared_import.total_bytes,
        duration_seconds=prepared_import.parse_result.duration_seconds,
        workspace_id=prepared_import.workspace_id,
        series_name=prepared_import.series_name,
        series_description=prepared_import.series_description,
        important_extra_data=prepared_import.important_extra_data,
        character_bible=prepared_import.character_bible,
    )


def _split_checkpoints(lease, windows):
    return {
        window.sequence_no: VideoChunkCheckpointState(
            job_id=lease.job_id,
            sequence_no=window.sequence_no,
            video_chunk_id=derive_video_chunk_id(
                lease.video_id,
                window.sequence_no,
            ),
            stage=VideoChunkCheckpointStage.SPLIT_READY.value,
            chunk_start_seconds=window.chunk_start_seconds,
            chunk_end_seconds=window.chunk_end_seconds,
            chunk_media_uri=f"artifact://test/chunks/{window.sequence_no}.mp4",
            analysis_window_media_uri=(
                f"artifact://test/windows/{window.sequence_no}.mp4"
            ),
        )
        for window in windows
    }


def _persistence(
    tmp_path: Path,
    prepared_import,
    *,
    lease=None,
    jobs=None,
    artifacts=None,
    checkpoints=None,
    videos=None,
    missing_legacy_background: bool = False,
    clock=lambda: 1_000_000_000,
):
    selected_lease = lease or _lease(prepared_import)
    windows = [chunk.window for chunk in prepared_import.parse_result.chunks]
    selected_checkpoints = (
        checkpoints
        if checkpoints is not None
        else _split_checkpoints(selected_lease, windows)
    )
    selected_jobs = jobs or _MemoryJobs(selected_checkpoints)
    selected_artifacts = artifacts or _MemoryArtifacts(tmp_path / "artifacts")
    selected_videos = videos or _VideoSink()
    attempt_directory = (
        selected_artifacts.root
        / selected_lease.job_id
        / "leases"
        / f"lease-{selected_lease.lease_version:08d}"
    )
    persistence = VideoParsePersistence(
        selected_jobs,  # type: ignore[arg-type]
        selected_videos,  # type: ignore[arg-type]
        selected_artifacts,  # type: ignore[arg-type]
        lease=selected_lease,
        attempt_directory=attempt_directory,
        checkpoints=selected_checkpoints,
        chunk_media_refs={
            sequence_no: checkpoint.chunk_media_uri
            for sequence_no, checkpoint in selected_checkpoints.items()
            if checkpoint.chunk_media_uri is not None
        },
        analysis_window_media_refs={
            sequence_no: checkpoint.analysis_window_media_uri
            for sequence_no, checkpoint in selected_checkpoints.items()
            if checkpoint.analysis_window_media_uri is not None
        },
        keyframes_by_sequence={
            window.sequence_no: (
                VideoChunkKeyframeReference(
                    uri=f"artifact://test/keyframes/{window.sequence_no}.jpg",
                    at_seconds=window.chunk_start_seconds,
                ),
            )
            for window in windows
        },
        config=VideoProcessingConfig(
            embedding_dimensions=3,
            chunk_duration_seconds=10,
            context_overlap_seconds=0,
            asr_mode="none",
        ),
        parse_config=None,
        missing_legacy_background=missing_legacy_background,
        processing_started_ns=1_000_000_000,
        monotonic_clock=clock,
    )
    return persistence, selected_jobs, selected_artifacts, selected_videos


@pytest.mark.parametrize("inconsistent", ["missing_pointer", "gap", "extra"])
def test_completed_parse_checkpoints_must_be_a_continuous_prefix(
    inconsistent,
    prepared_import,
    tmp_path: Path,
) -> None:
    lease = _lease(prepared_import)
    windows = [chunk.window for chunk in prepared_import.parse_result.chunks]

    def checkpoint(sequence_no, stage, uri=None):
        return VideoChunkCheckpointState(
            job_id=lease.job_id,
            sequence_no=sequence_no,
            video_chunk_id=derive_video_chunk_id(lease.video_id, sequence_no),
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
                "artifact://test/chunks/chunk-0001.json",
            ),
        }
    else:
        checkpoints = {
            0: checkpoint(0, VideoChunkCheckpointStage.SPLIT_READY.value),
            1: checkpoint(1, VideoChunkCheckpointStage.SPLIT_READY.value),
            2: checkpoint(2, VideoChunkCheckpointStage.SPLIT_READY.value),
        }
    persistence, _jobs, _artifacts, _videos = _persistence(
        tmp_path,
        prepared_import,
        lease=lease,
        checkpoints=checkpoints,
    )

    with pytest.raises(ArtifactIntegrityError):
        persistence.load_completed_chunks(
            windows,
            models=prepared_import.parse_result.models,
        )


def test_chunk_checkpoints_resume_from_db_and_record_processing_duration(
    prepared_import,
    tmp_path: Path,
) -> None:
    clock_values = iter([1_100_000_000, 1_250_000_000])
    persistence, jobs, artifacts, videos = _persistence(
        tmp_path,
        prepared_import,
        clock=lambda: next(clock_values),
    )
    result = prepared_import.parse_result
    windows = [chunk.window for chunk in result.chunks]
    assert result.models is not None

    assert persistence.load_completed_chunks(windows, models=result.models) == ()
    for chunk in result.chunks:
        persistence.save_chunk(chunk, models=result.models)
    persistence.save_parse_result(result)

    assert jobs.processing_durations == [500, 650]
    assert all(
        checkpoint.stage == VideoChunkCheckpointStage.PARSED.value
        and checkpoint.parse_artifact_uri is not None
        for checkpoint in jobs.by_sequence.values()
    )
    assert (
        artifacts.root
        / _lease(prepared_import).job_id
        / "leases"
        / "lease-00000001"
        / "pipeline.parse.json"
    ).is_file()

    resumed, _jobs, _artifacts, _videos = _persistence(
        tmp_path,
        prepared_import,
        jobs=jobs,
        artifacts=artifacts,
        checkpoints=dict(jobs.by_sequence),
        videos=videos,
    )
    restored = resumed.load_completed_chunks(windows, models=result.models)
    assert [chunk.window_summary for chunk in restored] == [
        chunk.window_summary for chunk in result.chunks
    ]


def test_stale_lease_cannot_write_a_parse_artifact(
    prepared_import,
    tmp_path: Path,
) -> None:
    persistence, jobs, artifacts, _videos = _persistence(
        tmp_path,
        prepared_import,
    )
    result = prepared_import.parse_result
    assert result.models is not None
    persistence.load_completed_chunks(
        [chunk.window for chunk in result.chunks],
        models=result.models,
    )
    jobs.move_to_lease(2)

    with pytest.raises(ParseJobLeaseLostError, match="fixture lease lost"):
        persistence.save_chunk(result.chunks[0], models=result.models)

    assert not list(artifacts.root.rglob("chunk-0000.json"))
    assert jobs.by_sequence[0].stage == VideoChunkCheckpointStage.SPLIT_READY.value


def test_embedding_cache_round_trips_and_rejects_corruption(
    prepared_import,
    tmp_path: Path,
) -> None:
    persistence, jobs, artifacts, _videos = _persistence(
        tmp_path,
        prepared_import,
    )
    invalid = ChunkEmbedding(
        sequence_no=0,
        modality="text",
        input_sha256="a" * 64,
        result=EmbeddingResult(
            model="embedding-test",
            modality="text",
            vector=[1.0, 2.0],
        ),
    )
    with pytest.raises(RuntimeError, match="unexpected vector dimension"):
        persistence.save_embedding(invalid)
    assert jobs.by_sequence[0].stage == VideoChunkCheckpointStage.SPLIT_READY.value
    assert not list(artifacts.root.rglob("*.text.json"))

    embedding = ChunkEmbedding(
        sequence_no=0,
        modality="text",
        input_sha256="a" * 64,
        result=EmbeddingResult(
            model="embedding-test",
            modality="text",
            vector=[1.0, 2.0, 3.0],
        ),
    )

    persistence.save_embedding(embedding)

    assert persistence.load_embedding(
        sequence_no=0,
        modality="text",
        input_sha256="a" * 64,
    ) == embedding
    checkpoint = jobs.by_sequence[0]
    assert checkpoint.stage == VideoChunkCheckpointStage.TEXT_EMBEDDED.value
    assert checkpoint.text_embedding_artifact_uri is not None
    path = artifacts.resolve(checkpoint.text_embedding_artifact_uri)
    path.write_text('{"corrupted": true}', encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError, match="committed text embedding"):
        persistence.load_embedding(
            sequence_no=0,
            modality="text",
            input_sha256="a" * 64,
        )


def test_late_old_lease_artifact_cannot_replace_the_committed_parse_pointer(
    prepared_import,
    tmp_path: Path,
    monkeypatch,
) -> None:
    result = prepared_import.parse_result
    assert result.models is not None
    window = result.chunks[0].window
    first_lease = _lease(prepared_import)
    second_lease = _lease(prepared_import, lease_version=2)
    checkpoints = _split_checkpoints(first_lease, [window])
    jobs = _MemoryJobs(checkpoints)
    artifacts = _MemoryArtifacts(tmp_path / "artifacts")
    old_persistence, _jobs, _artifacts, _videos = _persistence(
        tmp_path,
        prepared_import,
        lease=first_lease,
        jobs=jobs,
        artifacts=artifacts,
        checkpoints=dict(checkpoints),
    )
    new_persistence, _jobs, _artifacts, _videos = _persistence(
        tmp_path,
        prepared_import,
        lease=second_lease,
        jobs=jobs,
        artifacts=artifacts,
        checkpoints=dict(checkpoints),
    )
    old_persistence.load_completed_chunks([window], models=result.models)
    new_persistence.load_completed_chunks([window], models=result.models)

    from server.service import parse_persistence as persistence_module

    original_write = persistence_module._write_json_atomic
    old_write_started = Event()
    allow_old_write = Event()

    def block_old_parse_write(path, payload):
        if "lease-00000001" in path.parts and path.parent.name == "chunks":
            old_write_started.set()
            assert allow_old_write.wait(timeout=5)
        original_write(path, payload)

    monkeypatch.setattr(
        persistence_module,
        "_write_json_atomic",
        block_old_parse_write,
    )
    old_chunk = result.chunks[0].model_copy(
        update={"window_summary": "old lease"}
    )
    new_chunk = result.chunks[0].model_copy(
        update={"window_summary": "new lease"}
    )
    old_errors = []

    def save_old_chunk():
        try:
            old_persistence.save_chunk(old_chunk, models=result.models)
        except BaseException as exc:
            old_errors.append(exc)

    old_thread = Thread(target=save_old_chunk)
    old_thread.start()
    assert old_write_started.wait(timeout=5)
    jobs.move_to_lease(second_lease.lease_version)
    new_persistence.save_chunk(new_chunk, models=result.models)

    committed = jobs.by_sequence[0]
    assert committed.parse_artifact_uri is not None
    assert "lease-00000002" in committed.parse_artifact_uri
    allow_old_write.set()
    old_thread.join(timeout=5)

    assert not old_thread.is_alive()
    assert len(old_errors) == 1
    assert isinstance(old_errors[0], ParseJobLeaseLostError)
    committed_artifact = VideoChunkCheckpoint.model_validate_json(
        artifacts.resolve(committed.parse_artifact_uri).read_text(encoding="utf-8")
    )
    assert committed_artifact.chunk.window_summary == "new lease"
    old_path = (
        artifacts.root
        / first_lease.job_id
        / "leases"
        / "lease-00000001"
        / "chunks"
        / "chunk-0000.json"
    )
    old_artifact = VideoChunkCheckpoint.model_validate_json(
        old_path.read_text(encoding="utf-8")
    )
    assert old_artifact.chunk.window_summary == "old lease"


def test_persist_hands_complete_media_and_embeddings_to_the_video_repository(
    prepared_import,
    tmp_path: Path,
) -> None:
    persistence, jobs, _artifacts, videos = _persistence(
        tmp_path,
        prepared_import,
    )
    parse_result = prepared_import.parse_result
    assert parse_result.models is not None
    windows = [chunk.window for chunk in parse_result.chunks]
    persistence.load_completed_chunks(windows, models=parse_result.models)
    for chunk in parse_result.chunks:
        persistence.save_chunk(chunk, models=parse_result.models)

    documents = {
        document.sequence_no: document
        for document in build_video_chunk_retrieval_documents(parse_result)
    }
    text_embeddings = {
        sequence_no: ChunkEmbedding(
            sequence_no=sequence_no,
            modality="text",
            input_sha256=f"{sequence_no + 1:064x}",
            result=EmbeddingResult(
                model="embedding-test",
                modality="text",
                vector=[1.0, 2.0, 3.0],
            ),
        )
        for sequence_no in documents
    }
    vision_embeddings = {
        sequence_no: ChunkEmbedding(
            sequence_no=sequence_no,
            modality="video",
            input_sha256=f"{sequence_no + 11:064x}",
            result=EmbeddingResult(
                model="embedding-test",
                modality="video",
                vector=[3.0, 2.0, 1.0],
            ),
        )
        for sequence_no in documents
    }
    for embedding in text_embeddings.values():
        persistence.save_embedding(embedding)
    for embedding in vision_embeddings.values():
        persistence.save_embedding(embedding)

    video_chunk_ids = persistence.persist(
        ParsePipelineResult(
            parse_result=parse_result,
            documents=documents,
            text_embeddings=text_embeddings,
            vision_embeddings=vision_embeddings,
        )
    )

    assert video_chunk_ids == tuple(
        derive_video_chunk_id(prepared_import.video_id, sequence_no)
        for sequence_no in range(2)
    )
    assert videos.prepared is not None
    assert videos.lease.job_id == "11111111-1111-4111-8111-111111111111"
    assert videos.lease.lease_version == 1
    assert [
        chunk.chunk_media_ref for chunk in videos.prepared.chunks
    ] == [
        f"artifact://test/chunks/{sequence_no}.mp4"
        for sequence_no in range(2)
    ]
    assert [
        chunk.keyframes[0].at_seconds for chunk in videos.prepared.chunks
    ] == [0, 10]
    assert all(
        chunk.text_embedding.vector == [1.0, 2.0, 3.0]
        and chunk.vision_embedding.vector == [3.0, 2.0, 1.0]
        for chunk in videos.prepared.chunks
    )
    assert all(
        checkpoint.stage == VideoChunkCheckpointStage.VISION_EMBEDDED.value
        for checkpoint in jobs.by_sequence.values()
    )


def test_partial_legacy_parse_without_a_background_snapshot_cannot_resume(
    prepared_import,
    tmp_path: Path,
) -> None:
    persistence, _jobs, _artifacts, _videos = _persistence(
        tmp_path,
        prepared_import,
        missing_legacy_background=True,
    )
    result = prepared_import.parse_result
    assert result.models is not None

    with pytest.raises(RuntimeError, match="旧解析任务缺少背景快照"):
        persistence.load_completed_chunks(
            [chunk.window for chunk in result.chunks],
            models=result.models,
        )
