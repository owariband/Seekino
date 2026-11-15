from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.chat.common.errors import AgentExecutionTimeoutError, AgentModelInvocationError
from agent.chat.video_search.frame_evidence import (
    FrameReadError, FrameReadRequest, PointFrameReadRequest,
)
from engine.rag.contracts import VideoChunkEvidence
from server.model.dto.libraryDTO import VideoSummaryState
from server.model.video import VideoIndexStatus
from server.repository.library import SeriesNotFoundError
from server.service.video_parse_errors import VideoParseConflictError
from server.service.video_frame_reader import VideoFrameReader
from server.storage.artifact import LocalArtifactStore


@pytest.fixture
def frames(tmp_path, monkeypatch):
    artifacts = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    series_id, video_id = str(uuid4()), str(uuid4())
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-video")
    original = artifacts.publish_video_artifact(
        source, series_id=series_id, video_id=video_id, category="chunks", filename="source.mp4",
    )
    evidence = VideoChunkEvidence.model_construct(
        tenant_id="mine", series_id=series_id, video_id=video_id,
        video_chunk_id=str(uuid4()), original_source_uri=original.uri,
        content_hash=original.sha256, start_seconds=60_000, end_seconds=120_000,
        duration_seconds=240_000, parsed_at=1000,
        parse_provenance={"parseTaskId": str(uuid4()), "contentVersion": 2, "indexGeneration": 1},
    )
    repository = SimpleNamespace(evidence=evidence, calls=[], error=None)

    def hydrate(candidates):
        repository.calls.append(candidates)
        if repository.error:
            raise repository.error
        return (SimpleNamespace(evidence=repository.evidence),)

    repository.hydrate_retrieval_candidates = hydrate
    clock, extracted = [0.0], []
    monkeypatch.setattr("server.service.video_frame_reader.monotonic", lambda: clock[0])

    video = VideoSummaryState(
        video_id=video_id, tenant_id="mine", series_id=series_id, episode_order=1,
        upload_sequence=1, original_filename="source.mp4", original_source_uri=original.uri,
        inference_proxy_uri=None, content_hash=original.sha256, total_bytes=len(b"source-video"),
        duration_seconds=2_000_000, upload_status="COMPLETED", parse_status="COMPLETED",
        upload_progress=100, parse_progress=100, index_status=VideoIndexStatus.READY,
        last_error=None,
    )
    video_lookup = SimpleNamespace(video=video, calls=[], error=None, after_extract=None)

    def get_video(video_id, *, tenant_id):
        video_lookup.calls.append((video_id, tenant_id))
        if video_lookup.error:
            raise video_lookup.error
        return video_lookup.video

    def extract(path, timestamps, *, output_dir, max_height, timeout_seconds):
        assert path == artifacts.resolve(original.uri)
        assert max_height == 480
        extracted.append((list(timestamps), timeout_seconds))
        clock[0] += 1
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        frame = Path(output_dir) / "frame.jpg"
        frame.write_bytes(b"\xff\xd8\xff" + str(timestamps[0]).encode())
        if video_lookup.after_extract is not None:
            video_lookup.after_extract()
        return [(timestamps[0], frame)]

    monkeypatch.setattr("server.service.video_frame_reader.extract_video_frames", extract)
    request = FrameReadRequest(
        evidence=evidence, inspection_id=uuid4().hex, role="Query", conversation_id="conversation",
        message_id="message", start_ms=80_000, end_ms=84_000, frame_count=4,
        max_height=480, timeout_seconds=10, check_control=lambda: None,
    )
    point_request = PointFrameReadRequest(
        tenant_id="mine", allowed_series_ids=(series_id,), video_id=video_id,
        timestamp_ms=1_056_000, inspection_id=uuid4().hex, role="Query",
        conversation_id="conversation", message_id="message", max_height=480,
        timeout_seconds=10, check_control=lambda: None,
    )
    return SimpleNamespace(
        reader=VideoFrameReader(repository, artifacts, get_video=get_video),
        artifacts=artifacts, repository=repository, request=request,
        point_request=point_request, video_lookup=video_lookup,
        extracted=extracted, clock=clock,
    )


def test_absolute_millisecond_frames_and_durable_manifest(frames):
    result = frames.reader(frames.request)
    assert frames.extracted == [([80_500], 10), ([81_500], 9), ([82_500], 8), ([83_500], 7)]
    assert len(result.artifact.images) == 4
    assert all(image.data_url.startswith("data:image/jpeg;base64,") for image in result.artifact.images)
    payload = result.payload
    assert payload["frame_count"] == 4 and payload["images_supplied"] and not payload["cache_hit"]
    assert payload["source_identity"]["content_version"] == 2
    assert payload["start_ms"] == 80_000 and payload["end_ms"] == 84_000
    for index, frame in enumerate(payload["frames"]):
        data = frames.artifacts.resolve(frame["artifact_uri"]).read_bytes()
        assert frame["index"] == index
        assert frame["sha256"] == hashlib.sha256(data).hexdigest()
        assert frame["size_bytes"] == len(data)
        assert "actual_timestamp_ms" not in frame
    manifest = frames.artifacts.resolve(payload["manifest_uri"]).read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == payload["manifest_sha256"]
    saved = json.loads(manifest)
    assert saved == {key: value for key, value in payload.items()
                     if key not in {"manifest_uri", "manifest_sha256", "images_supplied"}}
    assert saved["images_prepared"] and "images_supplied" not in saved
    assert saved["role"] == "Query" and saved["message_id"] == "message"
    assert saved["source_identity"]["source_uri"] == frames.request.evidence.original_source_uri


def test_cached_frames_revalidate_source_and_write_a_new_inspection(frames):
    first = frames.reader(frames.request)
    second_request = replace(frames.request, inspection_id=uuid4().hex, role="Evaluator", message_id="review")
    second = frames.reader(second_request, first)
    assert len(frames.repository.calls) == 2 and len(frames.extracted) == 4
    assert second.payload["cache_hit"] and second.payload["frames"] == first.payload["frames"]
    assert second.payload["manifest_uri"] != first.payload["manifest_uri"]
    saved = json.loads(frames.artifacts.resolve(second.payload["manifest_uri"]).read_text())
    assert saved["role"] == "Evaluator" and saved["message_id"] == "review"
    frames.repository.error = VideoParseConflictError("version changed")
    with pytest.raises(FrameReadError) as failure:
        frames.reader(replace(second_request, inspection_id=uuid4().hex), first)
    assert failure.value.code == "FRAME_SOURCE_CHANGED" and not failure.value.retryable


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "other"), ("series_id", "other"), ("video_id", "other"),
    ("original_source_uri", "untrusted.mp4"), ("content_hash", "changed"),
    ("start_seconds", 80_001),
    ("parse_provenance", {"parseTaskId": "other", "contentVersion": 3, "indexGeneration": 1}),
])
def test_current_source_must_match_the_retrieved_evidence(frames, field, value):
    frames.repository.evidence = frames.request.evidence.model_copy(update={field: value})
    with pytest.raises(FrameReadError) as failure:
        frames.reader(frames.request)
    assert failure.value.code == "FRAME_SOURCE_CHANGED" and not failure.value.retryable
    assert frames.extracted == []


def test_invalid_repository_identity_is_source_feedback(frames):
    frames.repository.error = ValueError("video_chunk_id must be a canonical UUID")
    with pytest.raises(FrameReadError) as failure:
        frames.reader(frames.request)
    assert failure.value.code == "FRAME_SOURCE_UNAVAILABLE"
    assert frames.extracted == []


def test_range_is_checked_against_canonical_chunk(frames):
    with pytest.raises(ValueError, match="canonical"):
        replace(frames.request, start_ms=59_999)
    with pytest.raises(ValueError, match="canonical"):
        replace(frames.request, end_ms=120_001)
    with pytest.raises(ValueError, match="distinct millisecond"):
        replace(frames.request, end_ms=80_001)
    assert frames.extracted == []


@pytest.mark.parametrize("error", [AgentModelInvocationError("cancelled"), AgentExecutionTimeoutError("deadline")])
def test_control_failure_between_frames_is_not_wrapped(frames, error):
    def check():
        if frames.extracted:
            raise error
    with pytest.raises(type(error)) as failure:
        frames.reader(replace(frames.request, check_control=check))
    assert failure.value is error and len(frames.extracted) == 1


def test_repository_time_counts_against_the_same_deadline(frames):
    hydrate = frames.repository.hydrate_retrieval_candidates
    def delayed(candidates):
        frames.clock[0] += 3
        return hydrate(candidates)
    frames.repository.hydrate_retrieval_candidates = delayed
    with pytest.raises(TimeoutError):
        frames.reader(replace(frames.request, timeout_seconds=2))
    assert frames.extracted == []


def test_extractor_timeout_is_not_converted_to_tool_feedback(frames, monkeypatch):
    failure = subprocess.TimeoutExpired("ffmpeg", 1)
    def stalled(*args, **kwargs):
        raise failure
    monkeypatch.setattr("server.service.video_frame_reader.extract_video_frames", stalled)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        frames.reader(frames.request)
    assert raised.value is failure


def test_missing_media_is_explicit_feedback(frames):
    frames.artifacts.resolve(frames.request.evidence.original_source_uri).unlink()
    with pytest.raises(FrameReadError) as failure:
        frames.reader(frames.request)
    assert failure.value.code == "FRAME_MEDIA_UNAVAILABLE" and failure.value.retryable


def test_cached_image_integrity_is_checked_before_reuse(frames):
    first = frames.reader(frames.request)
    frames.artifacts.resolve(first.payload["frames"][0]["artifact_uri"]).write_bytes(b"changed")
    with pytest.raises(FrameReadError) as failure:
        frames.reader(replace(frames.request, inspection_id=uuid4().hex), first)
    assert failure.value.code == "FRAME_SOURCE_UNAVAILABLE"
    assert len(frames.extracted) == 4


def test_extractor_failure_is_retryable_media_feedback(frames, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("ffmpeg cannot decode source")
    monkeypatch.setattr("server.service.video_frame_reader.extract_video_frames", failed)
    with pytest.raises(FrameReadError) as failure:
        frames.reader(frames.request)
    assert failure.value.code == "FRAME_MEDIA_UNAVAILABLE" and failure.value.retryable


def test_real_frames_use_absolute_source_times(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required for the local frame smoke test")
    from PIL import Image

    source = tmp_path / "red-then-blue.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x64:r=2:d=4",
        "-f", "lavfi", "-i", "color=blue:s=64x64:r=2:d=4",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[out]", "-map", "[out]",
        "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-y", str(source),
    ], check=True, capture_output=True, timeout=30)
    artifacts = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    series_id, video_id = str(uuid4()), str(uuid4())
    original = artifacts.publish_video_artifact(
        source, series_id=series_id, video_id=video_id, category="chunks", filename="source.mp4",
    )
    evidence = VideoChunkEvidence.model_construct(
        tenant_id="mine", series_id=series_id, video_id=video_id, video_chunk_id=str(uuid4()),
        original_source_uri=original.uri, content_hash=original.sha256,
        start_seconds=2000, end_seconds=8000, duration_seconds=8000,
        parse_provenance={"parseTaskId": str(uuid4()), "contentVersion": 2, "indexGeneration": 1},
    )
    repository = SimpleNamespace(
        hydrate_retrieval_candidates=lambda candidates: (SimpleNamespace(evidence=evidence),),
    )
    result = VideoFrameReader(repository, artifacts)(FrameReadRequest(
        evidence=evidence, inspection_id=uuid4().hex, role="Query", conversation_id="conversation",
        message_id="message", start_ms=2000, end_ms=8000, frame_count=3,
        max_height=64, timeout_seconds=30, check_control=lambda: None,
    ))
    assert [frame["requested_timestamp_ms"] for frame in result.payload["frames"]] == [3000, 5000, 7000]
    assert len(result.artifact.images) == 3
    for frame, expected_channel in zip(result.payload["frames"], [0, 2, 2], strict=True):
        with Image.open(artifacts.resolve(frame["artifact_uri"])) as image:
            rgb = image.convert("RGB").getpixel((32, 32))
        assert rgb[expected_channel] > 200
        assert all(channel < 40 for index, channel in enumerate(rgb) if index != expected_channel)


def test_point_frame_extracts_absolute_1056000_without_chunk_hydration(frames):
    result = frames.reader.read_point(frames.point_request)

    assert frames.extracted == [([1_056_000], 10)]
    assert frames.repository.calls == []
    assert len(result.artifact.images) == 1
    assert all(image.data_url.startswith("data:image/jpeg;base64,") for image in result.artifact.images)
    payload = result.payload
    assert payload["source_kind"] == "source_video_frame"
    assert payload["video_id"] == frames.video_lookup.video.video_id
    assert payload["series_id"] == frames.video_lookup.video.series_id
    assert payload["requested_timestamp_ms"] == 1_056_000
    assert payload["frame_count"] == 1 and payload["images_supplied"] is True
    assert payload["source_identity"]["content_hash"] == frames.video_lookup.video.content_hash
    frame = payload["frames"][0]
    assert frame["requested_timestamp_ms"] == 1_056_000
    assert frame["actual_timestamp_ms"] is None
    assert payload["images_prepared"] is True and "actual_timestamp_ms" not in payload
    manifest = frames.artifacts.resolve(payload["manifest_uri"]).read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == payload["manifest_sha256"]
    saved = json.loads(manifest)
    assert saved == {key: value for key, value in payload.items()
                     if key not in {"manifest_uri", "manifest_sha256", "images_supplied"}}


def test_point_frame_rejects_cross_series_and_cross_tenant(frames):
    with pytest.raises(FrameReadError) as failure:
        frames.reader.read_point(replace(
            frames.point_request, allowed_series_ids=(str(uuid4()),),
        ))
    assert failure.value.code == "FRAME_SCOPE_MISMATCH" and not failure.value.retryable

    frames.video_lookup.video = replace(frames.video_lookup.video, tenant_id="other")
    with pytest.raises(FrameReadError) as failure:
        frames.reader.read_point(frames.point_request)
    assert failure.value.code == "FRAME_SCOPE_MISMATCH" and not failure.value.retryable

    frames.video_lookup.error = SeriesNotFoundError("video does not exist in this workspace")
    with pytest.raises(FrameReadError) as failure:
        frames.reader.read_point(frames.point_request)
    assert failure.value.code == "FRAME_SCOPE_MISMATCH"
    assert frames.extracted == []


def test_point_frame_reads_uploaded_but_non_ready_video(frames):
    frames.video_lookup.video = replace(
        frames.video_lookup.video,
        upload_status="UPLOADED", parse_status="FAILED", index_status=VideoIndexStatus.FAILED,
    )
    result = frames.reader.read_point(frames.point_request)

    assert result.payload["frame_count"] == 1
    assert frames.video_lookup.video.parse_status == "FAILED"
    assert frames.extracted == [([1_056_000], 10)]

    frames.video_lookup.video = replace(frames.video_lookup.video, upload_status="UPLOADING")
    with pytest.raises(FrameReadError) as failure:
        frames.reader.read_point(replace(frames.point_request, inspection_id=uuid4().hex))
    assert failure.value.code == "FRAME_MEDIA_UNAVAILABLE" and failure.value.retryable


def test_point_frame_missing_uploaded_media_is_retryable_feedback(frames):
    frames.artifacts.resolve(frames.video_lookup.video.original_source_uri).unlink()
    with pytest.raises(FrameReadError) as failure:
        frames.reader.read_point(frames.point_request)
    assert failure.value.code == "FRAME_MEDIA_UNAVAILABLE" and failure.value.retryable
    assert frames.extracted == []


@pytest.mark.parametrize("timestamp_ms", [0, 1_999_999])
def test_point_frame_accepts_times_inside_source_duration(frames, timestamp_ms):
    result = frames.reader.read_point(replace(
        frames.point_request, timestamp_ms=timestamp_ms,
    ))
    assert result.payload["requested_timestamp_ms"] == timestamp_ms
    assert frames.extracted == [([timestamp_ms], 10)]


@pytest.mark.parametrize("timestamp_ms", [2_000_000, 2_000_001])
def test_point_frame_rejects_times_at_or_beyond_source_duration(frames, timestamp_ms):
    with pytest.raises(FrameReadError) as failure:
        frames.reader.read_point(replace(
            frames.point_request, timestamp_ms=timestamp_ms,
        ))
    assert failure.value.code == "INVALID_FRAME_REQUEST"
    assert frames.extracted == []


@pytest.mark.parametrize("timestamp_ms", [-1, True, 1.5, "1056000"])
def test_point_request_rejects_noninteger_or_negative_timestamps(frames, timestamp_ms):
    with pytest.raises(ValueError):
        replace(frames.point_request, timestamp_ms=timestamp_ms)
    assert frames.extracted == []


@pytest.mark.parametrize("field,value", [
    ("content_hash", "changed"),
    ("original_source_uri", "artifact://test/media/series/other/videos/other/chunks/source.mp4"),
    ("duration_seconds", 2_000_001),
])
def test_point_frame_rejects_source_changes_during_extraction(frames, field, value):
    def changed() -> None:
        frames.video_lookup.video = replace(frames.video_lookup.video, **{field: value})

    frames.video_lookup.after_extract = changed
    with pytest.raises(FrameReadError) as failure:
        frames.reader.read_point(frames.point_request)
    assert failure.value.code == "FRAME_SOURCE_CHANGED" and not failure.value.retryable
    assert len(frames.extracted) == 1


def test_point_cached_frames_revalidate_source_and_image_fingerprint(frames):
    first = frames.reader.read_point(frames.point_request)
    second = frames.reader.read_point(
        replace(frames.point_request, inspection_id=uuid4().hex, role="Evaluator"), first,
    )
    assert second.payload["cache_hit"] is True
    assert second.payload["frames"] == first.payload["frames"]
    assert len(frames.extracted) == 1
    assert frames.video_lookup.calls == [
        (frames.point_request.video_id, "mine"),
        (frames.point_request.video_id, "mine"),
        (frames.point_request.video_id, "mine"),
        (frames.point_request.video_id, "mine"),
    ]

    frames.artifacts.resolve(second.payload["frames"][0]["artifact_uri"]).write_bytes(b"changed")
    with pytest.raises(FrameReadError) as failure:
        frames.reader.read_point(
            replace(frames.point_request, inspection_id=uuid4().hex), second,
        )
    assert failure.value.code == "FRAME_SOURCE_UNAVAILABLE" and failure.value.retryable
    assert len(frames.extracted) == 1


def test_point_cached_frames_reject_changed_source(frames):
    first = frames.reader.read_point(frames.point_request)
    frames.video_lookup.video = replace(
        frames.video_lookup.video, content_hash="changed",
    )
    with pytest.raises(FrameReadError) as failure:
        frames.reader.read_point(
            replace(frames.point_request, inspection_id=uuid4().hex), first,
        )
    assert failure.value.code == "FRAME_SOURCE_CHANGED" and not failure.value.retryable
    assert len(frames.extracted) == 1


def test_point_control_failure_after_extraction_is_not_wrapped(frames):
    error = AgentModelInvocationError("cancelled")

    def check() -> None:
        if frames.extracted:
            raise error

    with pytest.raises(AgentModelInvocationError) as failure:
        frames.reader.read_point(replace(frames.point_request, check_control=check))
    assert failure.value is error and len(frames.extracted) == 1
