from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from engine.parse.schemas import VideoParseResult
from gateway.schemas import EmbeddingResult
from sqlalchemy import select

from server.database import create_session_factory
from server.model.index_sync import IndexSyncIntentRecord, IndexSyncStatus
from server.model.job import ParseJobStatus, VideoChunkCheckpointStage
from server.model.video import (
    SeriesRecord,
    VideoChunkEmbeddingRecord,
    VideoChunkRecord,
)
from server.repository.index_sync import IndexSyncRepository
from server.repository.job import ParseJobRepository
from server.repository.library import LibraryRepository
from server.repository.upload import UploadRepository
from server.repository.video import VideoRepository
from server.service.index_sync import IndexSyncService
from server.service.library import LibraryService
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore
from server.worker.processor import VideoProcessingConfig, VideoProcessingService


class _Parser:
    def __init__(self, template: VideoParseResult) -> None:
        self.template = template

    def run(
        self,
        source_video,
        *,
        duration_ms,
        output_dir,
        series_bible,
        on_windows_ready,
        on_chunk_complete,
    ) -> VideoParseResult:
        output = Path(output_dir)
        chunks = []
        for chunk in self.template.chunks:
            sequence_no = chunk.window.sequence_no
            media_path = output / "windows" / f"chunk-{sequence_no:04d}.mp4"
            media_path.parent.mkdir(parents=True, exist_ok=True)
            media_path.write_bytes(f"analysis-{sequence_no}".encode())
            window = chunk.window.model_copy(
                update={
                    "source_video": Path(source_video),
                    "media_path": media_path,
                }
            )
            chunks.append(chunk.model_copy(update={"window": window}))
        result = self.template.model_copy(
            update={
                "source_video": Path(source_video),
                "duration_ms": duration_ms,
                "chunks": chunks,
            }
        )
        on_windows_ready([item.window for item in chunks])
        for chunk in chunks:
            path = output / "chunks" / f"chunk-{chunk.window.sequence_no:04d}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(chunk.model_dump_json(), encoding="utf-8")
            on_chunk_complete(chunk, path)
        (output / "pipeline.parse.json").write_text(
            result.model_dump_json(),
            encoding="utf-8",
        )
        return result


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


def test_processing_service_reuses_engine_outputs_and_reaches_ready(
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
        checksum_sha256=hashlib.sha256(source_bytes).hexdigest(),
        start_byte=0,
        end_byte_exclusive=len(source_bytes),
    )
    completed = upload.complete(upload_session.upload_session_id)
    jobs = ParseJobRepository(session_factory)
    lease = jobs.claim_next(
        "worker-a",
        lease_duration=timedelta(minutes=5),
    )
    assert lease is not None
    assert lease.job_id == completed.job_id

    def materialize(windows, *, output_dir, **_kwargs):
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for window in windows:
            path = directory / f"chunk-{window.sequence_no:04d}.mp4"
            path.write_bytes(f"canonical-{window.sequence_no}".encode())
            paths.append(path)
        return paths

    monkeypatch.setattr(
        "server.worker.processor.materialize_canonical_chunks",
        materialize,
    )
    embedder = _Embedder()
    index_store = _IndexStore()
    processor = VideoProcessingService(
        jobs,
        VideoRepository(session_factory),
        IndexSyncService(IndexSyncRepository(session_factory), index_store),  # type: ignore[arg-type]
        artifacts,
        _Parser(prepared_import.parse_result),  # type: ignore[arg-type]
        embedder,
        config=VideoProcessingConfig(embedding_dimensions=3),
    )

    video_chunk_ids = processor.process(lease)
    repeated_video_chunk_ids = processor.process(lease)
    assert jobs.mark_succeeded(lease)

    assert repeated_video_chunk_ids == video_chunk_ids
    assert len(video_chunk_ids) == 2
    assert len(embedder.text_inputs) == 2
    assert all("第" in text for text in embedder.text_inputs)
    assert [path.name for path in embedder.video_inputs] == [
        "chunk-0000.mp4",
        "chunk-0001.mp4",
    ]
    assert len(index_store.points) == 2
    checkpoints = jobs.list_checkpoints(completed.job_id)
    assert {item.stage for item in checkpoints} == {
        VideoChunkCheckpointStage.INDEXED.value
    }
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
    assert all(item.text_vector == [1.0, 2.0, 3.0] for item in embeddings)
    assert all(item.vision_vector == [3.0, 2.0, 1.0] for item in embeddings)
    assert all(item.status == IndexSyncStatus.SYNCED.value for item in intents)
