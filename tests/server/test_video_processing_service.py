from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest
from PIL import Image
from engine.parsev1.pipeline import ParsePipelineInput
from engine.parsev1.runtime import VideoParseRuntimeConfig
from gateway.config import load_config

from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig
from server.config.video_processing import VideoProcessingConfig
from server.identity import derive_video_chunk_id
from server.model.dto.jobDTO import ParseJobLease
from server.model.job import ParseJobStage, VideoChunkCheckpointStage
from server.mq.contracts import ParseJobLeaseLostError
from server.service.parse_persistence import VideoParsePersistence
from server.service.upload import UploadServiceConfig
from server.service.video_processing import VideoProcessingService
from server.utils.files import write_json_once
from server.utils.time import now_unix_time_ms, timedelta_to_ms


def test_worker_uses_the_config_snapshot_instead_of_its_startup_processor() -> None:
    selected = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(),
        gateway=load_config(),
        upload=UploadServiceConfig(),
        images=ImageUploadConfig(),
        embedding_model="test-embedding",
        embedding_dimensions=1024,
        embedding_fps=1,
    )
    observed = []

    class ConfiguredProcessor:
        def process(self, lease):
            observed.append(lease.job_id)
            return ["configured-chunk"]

    def build(config):
        observed.append(config.window.analysis_max_height)
        return ConfiguredProcessor()

    processor = VideoProcessingService(
        object(),
        object(),
        object(),
        object(),
        object(),
        processor_factory=build,
    )
    lease = SimpleNamespace(
        job_id="configured-job",
        parse_config=selected.model_dump(mode="json"),
    )

    assert processor.process(lease) == ["configured-chunk"]
    assert observed == [720, "configured-job"]


def test_background_limits_are_applied_and_reused_from_the_job_snapshot(
    tmp_path: Path,
) -> None:
    selected = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(),
        gateway=load_config(),
        upload=UploadServiceConfig(),
        images=ImageUploadConfig(),
        embedding_model="test-embedding",
        embedding_dimensions=1024,
        embedding_fps=1,
    )
    selected = selected.model_copy(
        update={
            "context": selected.context.model_copy(
                update={
                    "background_text_max_chars": 256,
                    "max_background_images": 1,
                    "background_image_max_edge": 128,
                }
            ),
        }
    )
    image_paths = {}
    for image_id in ("first", "second"):
        path = tmp_path / f"{image_id}.png"
        Image.new("RGB", (512, 256), "blue").save(path)
        image_paths[image_id] = path

    class Artifacts:
        def resolve_series_image(self, image_id, **_kwargs):
            return image_paths[image_id]

        def uri_for_path(self, path):
            return str(path)

        def fingerprint(self, path):
            return SimpleNamespace(
                sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()
            )

    processor = VideoProcessingService(
        _MemoryJobs(),
        object(),
        object(),
        Artifacts(),
        object(),
        parse_config=selected,
    )
    lease = replace(
        _lease(),
        important_extra_data={
            "format": "markdown",
            "content": "asset://first asset://second " + "背景资料" * 100,
        },
    )
    work_directory = tmp_path / "work"
    work_directory.mkdir()

    text, references, missing = processor._parse_background(
        lease,
        work_directory,
        {},
    )

    assert not missing
    assert text is not None and len(text) == 256
    assert [item.image_id for item in references] == ["first"]
    with Image.open(references[0].path) as image:
        assert image.size == (128, 64)
    snapshot = json.loads(
        (work_directory / "background.snapshot.json").read_text()
    )
    assert snapshot["version"] == 2
    assert len(snapshot["images"]) == 1

    changed = replace(
        lease,
        important_extra_data={"format": "markdown", "content": "新背景"},
    )
    assert processor._parse_background(changed, work_directory, {}) == (
        text,
        references,
        False,
    )

    no_background = selected.model_copy(
        update={
            "context": selected.context.model_copy(
                update={
                    "background_text_enabled": False,
                    "background_images_enabled": False,
                }
            ),
        }
    )
    no_background_processor = VideoProcessingService(
        _MemoryJobs(),
        object(),
        object(),
        Artifacts(),
        object(),
        parse_config=no_background,
    )
    disabled_directory = tmp_path / "disabled"
    disabled_directory.mkdir()
    invalid_unused_background = replace(
        lease,
        important_extra_data={"format": "json", "content": "{invalid"},
    )
    assert no_background_processor._parse_background(
        invalid_unused_background,
        disabled_directory,
        {},
    ) == (None, [], False)


class _MemoryJobs:
    def __init__(self) -> None:
        self.current = True
        self.by_sequence = {}
        self.progress_stages = []

    def list_checkpoints(self, _job_id):
        return [self.by_sequence[key] for key in sorted(self.by_sequence)]

    def assert_current_lease(self, _lease):
        if not self.current:
            raise ParseJobLeaseLostError("fixture lease lost")

    def update_progress(self, lease, *, stage):
        self.assert_current_lease(lease)
        self.progress_stages.append(ParseJobStage(stage))
        return True

    def save_checkpoint(
        self,
        lease,
        checkpoint,
        *,
        processing_duration_ms=None,
    ):
        del processing_duration_ms
        self.assert_current_lease(lease)
        current = self.by_sequence.get(checkpoint.sequence_no)
        if current is not None:
            checkpoint = _merge_checkpoint(current, checkpoint)
        self.by_sequence[checkpoint.sequence_no] = checkpoint
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

    def save_checkpoint(
        self,
        lease,
        checkpoint,
        *,
        processing_duration_ms=None,
    ):
        del processing_duration_ms
        self.assert_current_lease(lease)
        with self.lock:
            current = self.by_sequence.get(checkpoint.sequence_no)
            if current is not None:
                checkpoint = _merge_checkpoint(current, checkpoint)
            self.by_sequence[checkpoint.sequence_no] = checkpoint
            return checkpoint

    def move_to_lease(self, lease_version):
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

    def resolve(self, uri):
        try:
            return self.paths[uri]
        except KeyError as exc:
            raise FileNotFoundError(uri) from exc

    def fingerprint(self, uri):
        path = self.resolve(uri)
        return SimpleNamespace(
            size_bytes=path.stat().st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )


class _RecordingPipeline:
    def __init__(self, video_chunk_ids: list[str]) -> None:
        self._video_chunk_ids = video_chunk_ids
        self.models = SimpleNamespace(transcript_mode="none")
        self.requests: list[ParsePipelineInput] = []
        self.persistences: list[VideoParsePersistence] = []

    def run(self, request, *, persistence):
        self.requests.append(request)
        self.persistences.append(persistence)
        return list(self._video_chunk_ids)


class _SuccessfulIndexSink:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def sync_video_chunks(self, video_chunk_ids):
        self.calls.append(list(video_chunk_ids))


def _lease() -> ParseJobLease:
    return ParseJobLease(
        job_id="11111111-1111-4111-8111-111111111111",
        series_id="22222222-2222-4222-8222-222222222222",
        video_id="33333333-3333-4333-8333-333333333333",
        pipeline_version="pipeline-v1",
        worker_id="consumer-test",
        lease_version=1,
        lease_expires_at=now_unix_time_ms()
        + timedelta_to_ms(timedelta(minutes=5)),
        attempt_count=1,
        max_attempts=3,
        processing_duration_base_ms=0,
        episode_order=1,
        upload_sequence=1,
        original_filename="episode.mp4",
        original_source_uri="artifact://test/original.mp4",
        content_hash="0" * 64,
        total_bytes=1,
        duration_seconds=20,
        workspace_id="local-default",
        series_name="Fixture",
        series_description=None,
        important_extra_data=None,
        character_bible=[],
    )


def _materialize_analysis_one(window, **_kwargs):
    window.media_path.parent.mkdir(parents=True, exist_ok=True)
    window.media_path.write_bytes(f"analysis-{window.sequence_no}".encode())


def _materialize_canonical_one(window, *, output_dir, **_kwargs):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"chunk-{window.sequence_no:04d}.mp4"
    path.write_bytes(f"canonical-{window.sequence_no}".encode())
    return path


def _extract_frames(video_path, timestamps_ms, *, output_dir, **_kwargs):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, timestamp_ms in enumerate(timestamps_ms):
        path = directory / f"frame-{index:03d}-{timestamp_ms:08d}ms.jpg"
        path.write_bytes(b"\xff\xd8\xff" + Path(video_path).read_bytes())
        frames.append((timestamp_ms, path))
    return frames


def _unit_processor(
    tmp_path: Path,
    monkeypatch,
    *,
    jobs,
    pipeline,
    index=None,
):
    artifacts = _MemoryArtifacts(tmp_path / "artifacts")
    index = index or _SuccessfulIndexSink()
    processor = VideoProcessingService(
        jobs,  # type: ignore[arg-type]
        object(),
        index,  # type: ignore[arg-type]
        artifacts,  # type: ignore[arg-type]
        pipeline=pipeline,
        config=VideoProcessingConfig(
            embedding_dimensions=3,
            chunk_duration_seconds=10,
            context_overlap_seconds=0,
            asr_mode="none",
        ),
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
    return processor, artifacts, index


def test_processing_service_materializes_pipeline_input_and_indexes_each_chunk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lease = _lease()
    video_chunk_ids = [
        derive_video_chunk_id(lease.video_id, sequence_no)
        for sequence_no in range(2)
    ]
    jobs = _MemoryJobs()
    pipeline = _RecordingPipeline(video_chunk_ids)
    processor, artifacts, index = _unit_processor(
        tmp_path,
        monkeypatch,
        jobs=jobs,
        pipeline=pipeline,
    )

    assert processor.process(lease) == video_chunk_ids

    [request] = pipeline.requests
    assert request.duration_seconds == lease.duration_seconds
    assert request.chunk_duration_seconds == 10
    assert request.context_overlap_seconds == 0
    assert [item.window.sequence_no for item in request.chunks] == [0, 1]
    assert [item.canonical_media_path.read_bytes() for item in request.chunks] == [
        b"canonical-0",
        b"canonical-1",
    ]
    assert [
        item.canonical_media_sha256 for item in request.chunks
    ] == [
        hashlib.sha256(f"canonical-{sequence_no}".encode()).hexdigest()
        for sequence_no in range(2)
    ]
    assert len(pipeline.persistences) == 1
    assert isinstance(pipeline.persistences[0], VideoParsePersistence)
    assert jobs.progress_stages == [ParseJobStage.PREPARING]
    assert index.calls == [[video_chunk_ids[0]], [video_chunk_ids[1]]]
    assert [
        jobs.by_sequence[sequence_no].stage for sequence_no in range(2)
    ] == [VideoChunkCheckpointStage.INDEXED.value] * 2
    published_identity = f"{lease.job_id}-lease-{lease.lease_version:08d}"
    assert all(
        published_identity in jobs.by_sequence[sequence_no].chunk_media_uri
        and published_identity
        in jobs.by_sequence[sequence_no].analysis_window_media_uri
        for sequence_no in range(2)
    )
    assert len(
        [uri for uri in artifacts.paths if "/keyframes/" in uri]
    ) == 2


def test_index_checkpoint_is_not_committed_after_the_lease_is_lost(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lease = _lease()
    jobs = _MemoryJobs()
    pipeline = _RecordingPipeline(
        [derive_video_chunk_id(lease.video_id, sequence_no) for sequence_no in range(2)]
    )

    class LeaseLosingIndex(_SuccessfulIndexSink):
        def sync_video_chunks(self, video_chunk_ids):
            super().sync_video_chunks(video_chunk_ids)
            jobs.current = False

    index = LeaseLosingIndex()
    processor, _artifacts, _index = _unit_processor(
        tmp_path,
        monkeypatch,
        jobs=jobs,
        pipeline=pipeline,
        index=index,
    )

    with pytest.raises(ParseJobLeaseLostError, match="fixture lease lost"):
        processor.process(lease)

    assert index.calls == [[derive_video_chunk_id(lease.video_id, 0)]]
    assert all(
        checkpoint.stage == VideoChunkCheckpointStage.SPLIT_READY.value
        for checkpoint in jobs.by_sequence.values()
    )


def test_late_old_lease_media_cannot_replace_new_lease_checkpoint_pointers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class ParsingReached(RuntimeError):
        pass

    class StopAtPipeline:
        models = SimpleNamespace(transcript_mode="none")

        def run(self, _request, *, persistence):
            del persistence
            raise ParsingReached

    jobs = _ConcurrentMemoryJobs()
    old_processor, old_artifacts, _old_index = _unit_processor(
        tmp_path,
        monkeypatch,
        jobs=jobs,
        pipeline=StopAtPipeline(),
    )
    new_processor, new_artifacts, _new_index = _unit_processor(
        tmp_path,
        monkeypatch,
        jobs=jobs,
        pipeline=StopAtPipeline(),
    )
    old_materialization_started = Event()
    allow_old_materialization = Event()

    def materialize_analysis(window, **_kwargs):
        old_attempt = "lease-00000001" in window.media_path.parts
        if old_attempt:
            old_materialization_started.set()
            assert allow_old_materialization.wait(timeout=5)
        label = "old" if old_attempt else "new"
        window.media_path.parent.mkdir(parents=True, exist_ok=True)
        window.media_path.write_bytes(
            f"{label}-analysis-{window.sequence_no}".encode()
        )

    def materialize_canonical(window, *, output_dir, **_kwargs):
        old_attempt = "lease-00000001" in Path(output_dir).parts
        label = "old" if old_attempt else "new"
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"chunk-{window.sequence_no:04d}.mp4"
        path.write_bytes(f"{label}-canonical-{window.sequence_no}".encode())
        return path

    monkeypatch.setattr(
        "server.service.video_processing.materialize_analysis_window",
        materialize_analysis,
    )
    monkeypatch.setattr(
        "server.service.video_processing.materialize_canonical_chunk",
        materialize_canonical,
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


def test_background_snapshot_publish_keeps_first_complete_version(
    tmp_path: Path,
) -> None:
    target = tmp_path / "background.snapshot.json"
    candidates = [
        {"content": f"背景 {index}", "images": list(range(100))}
        for index in range(8)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda data: write_json_once(target, data), candidates))

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved in candidates
    write_json_once(target, {"content": "later"})
    assert json.loads(target.read_text(encoding="utf-8")) == saved
    assert list(tmp_path.iterdir()) == [target]
