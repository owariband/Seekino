from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import tempfile
from time import monotonic

from pydantic import ValidationError

from agent.chat.common.errors import AgentModelInvocationError
from agent.chat.video_search.frame_evidence import FrameInspection, FrameReadError, FrameReadRequest
from engine.common.frame_sampler import extract_video_frames, plan_frame_timestamps
from engine.rag.contracts import VideoChunkRetrievalIdentity
from gateway.media import ToolImageArtifact

from .video_parse_persistence import VideoParseConflictError, VideoParsePersistenceService
from ..storage.artifact import ArtifactStore


class VideoFrameReader:
    """Read source frames pinned to a retrieved publication; never start parsing."""

    def __init__(self, persistence: VideoParsePersistenceService, artifacts: ArtifactStore) -> None:
        self._persistence = persistence
        self._artifacts = artifacts

    def __call__(
        self, request: FrameReadRequest, cached: FrameInspection | None = None,
    ) -> FrameInspection:
        timeout = request.timeout_seconds
        if timeout is not None and (isinstance(timeout, bool) or not math.isfinite(timeout)):
            raise FrameReadError("INVALID_FRAME_REQUEST", "Frame timeout must be finite.")
        deadline = None if timeout is None else monotonic() + timeout

        def remaining() -> float | None:
            request.check_control()
            seconds = None if deadline is None else deadline - monotonic()
            if seconds is not None and seconds <= 0:
                raise TimeoutError("Frame inspection reached its deadline")
            return seconds

        remaining()
        evidence = request.evidence
        provenance = evidence.parse_provenance or {}
        try:
            identity = VideoChunkRetrievalIdentity(
                tenant_id=evidence.tenant_id, series_id=evidence.series_id,
                video_id=evidence.video_id, parse_task_id=provenance["parseTaskId"],
                content_version=provenance["contentVersion"], index_generation=provenance["indexGeneration"],
            )
        except (KeyError, ValidationError) as error:
            raise FrameReadError("FRAME_SOURCE_UNAVAILABLE", "Frame source lacks a formal retrieval identity.") from error
        try:
            hydrated = self._persistence.hydrate_retrieval_candidates([(evidence.video_chunk_id, identity)])
        except VideoParseConflictError as error:
            raise FrameReadError(
                "FRAME_SOURCE_CHANGED",
                "Source publication changed during this Kick; use another unchanged retrieved Chunk or request a new Chat turn.",
            ) from error
        except ValueError as error:
            raise FrameReadError("FRAME_SOURCE_UNAVAILABLE", "Frame source has an invalid retrieval identity.") from error
        remaining()
        if len(hydrated) != 1:
            raise FrameReadError("FRAME_SOURCE_UNAVAILABLE", "Frame source was not found.")
        current = hydrated[0].evidence
        fields = (
            "tenant_id", "series_id", "video_id", "video_chunk_id", "original_source_uri",
            "content_hash", "start_seconds", "end_seconds", "duration_seconds", "parse_provenance",
        )
        if any(getattr(current, field) != getattr(evidence, field) for field in fields):
            raise FrameReadError(
                "FRAME_SOURCE_CHANGED",
                "Source publication changed during this Kick; use another unchanged retrieved Chunk or request a new Chat turn.",
            )
        if not 0 <= current.start_seconds <= request.start_ms < request.end_ms <= current.end_seconds <= current.duration_seconds:
            raise FrameReadError("INVALID_FRAME_REQUEST", "Frame range is outside the current canonical Chunk.")
        source_identity = {
            **identity.model_dump(), "video_chunk_id": current.video_chunk_id,
            "content_hash": current.content_hash, "source_uri": current.original_source_uri,
            "chunk_start_ms": current.start_seconds, "chunk_end_ms": current.end_seconds,
        }
        duration = request.end_ms - request.start_ms
        timestamps = [request.start_ms + timestamp for timestamp in plan_frame_timestamps(
            duration, frame_fps=request.frame_count * 1000 / duration, max_frames=request.frame_count,
        )]
        cache_hit = bool(cached is not None and cached.payload.get("source_identity") == source_identity
                         and cached.payload.get("start_ms") == request.start_ms
                         and cached.payload.get("end_ms") == request.end_ms
                         and cached.payload.get("max_height") == request.max_height
                         and [frame["requested_timestamp_ms"] for frame in cached.payload.get("frames", [])] == timestamps)
        frames = deepcopy(cached.payload["frames"]) if cache_hit and cached is not None else []
        try:
            if cache_hit:
                for frame in frames:
                    remaining()
                    stored = self._artifacts.fingerprint(frame["artifact_uri"])
                    if stored.sha256 != frame["sha256"] or stored.size_bytes != frame["size_bytes"]:
                        raise FrameReadError("FRAME_SOURCE_UNAVAILABLE", "Cached frame bytes no longer match their provenance.", retryable=True)
            else:
                source = self._artifacts.resolve(current.original_source_uri)
                with tempfile.TemporaryDirectory(prefix="anivag-frame-inspection-") as directory:
                    for index, timestamp in enumerate(timestamps):
                        # Each extraction consumes the same deadline, including prior DB and I/O work.
                        extracted = extract_video_frames(
                            source, [timestamp], output_dir=Path(directory) / str(index),
                            max_height=request.max_height, timeout_seconds=remaining(),
                        )
                        remaining()
                        if len(extracted) != 1 or extracted[0][0] != timestamp:
                            raise FrameReadError("FRAME_SOURCE_UNAVAILABLE", "Extractor returned an unexpected frame position.", retryable=True)
                        stored = self._artifacts.publish_video_artifact(
                            extracted[0][1], series_id=current.series_id, video_id=current.video_id,
                            category="keyframes", filename=f"inspection-{request.inspection_id}-{index}.jpg",
                        )
                        frames.append({
                            "index": index, "requested_timestamp_ms": timestamp,
                            "artifact_uri": stored.uri, "sha256": stored.sha256, "size_bytes": stored.size_bytes,
                        })
            remaining()
            artifact = ToolImageArtifact.from_paths([
                (self._artifacts.resolve(frame["artifact_uri"]),
                 f"Source-video frame {frame['index']} requested at {frame['requested_timestamp_ms']} ms.")
                for frame in frames
            ])
            payload = {
                "inspection_id": request.inspection_id, "role": request.role,
                "conversation_id": request.conversation_id, "message_id": request.message_id,
                "created_at": datetime.now(UTC).isoformat(), "video_chunk_id": current.video_chunk_id,
                "frame_count": len(frames), "start_ms": request.start_ms, "end_ms": request.end_ms,
                "max_height": request.max_height, "source_identity": source_identity,
                "sampling": "uniform_midpoints", "time_unit": "ms", "frames": frames,
                "cache_hit": cache_hit, "images_prepared": bool(artifact.images),
                "limitations": ["Timestamps are requested seek positions, not measured frame PTS.",
                                "Still frames contain no audio and do not prove continuous motion."],
            }
            remaining()
            with tempfile.TemporaryDirectory(prefix="anivag-frame-manifest-") as directory:
                manifest_path = Path(directory) / "manifest.json"
                manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                manifest = self._artifacts.publish_video_artifact(
                    manifest_path, series_id=current.series_id, video_id=current.video_id,
                    category="keyframes", filename=f"inspection-{request.inspection_id}.json",
                )
            remaining()
        except (TimeoutError, AgentModelInvocationError):
            raise
        except (OSError, RuntimeError) as error:
            raise FrameReadError("FRAME_MEDIA_UNAVAILABLE", "Source frames could not be read or saved.", retryable=True) from error
        except ValueError as error:
            raise FrameReadError("FRAME_MEDIA_UNAVAILABLE", "Source frames exceed the image budget or media contract.") from error
        # The durable manifest records preparation, not a provider acknowledgement.
        payload.update({"manifest_uri": manifest.uri, "manifest_sha256": manifest.sha256,
                        "images_supplied": bool(artifact.images)})
        return FrameInspection(payload=payload, artifact=artifact)
