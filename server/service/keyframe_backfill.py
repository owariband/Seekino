from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path

from engine.common.frame_sampler import extract_video_frames

from ..model.dto.keyframeDTO import (
    KeyframeBackfillCandidate,
    KeyframeBackfillIssue,
    KeyframeBackfillReport,
)
from ..repository.video import VideoRepository
from ..schema import VideoChunkKeyframeReference
from ..storage.artifact import ArtifactStore
from ..utils.media import (
    image_media_type,
    stored_keyframe_at_seconds,
    stored_keyframe_uri,
)

_logger = logging.getLogger(__name__)


class KeyframeBackfillService:
    """Enrich historical VideoChunks without rerunning model or index work."""

    def __init__(
        self,
        videos: VideoRepository,
        artifacts: ArtifactStore,
        *,
        max_height: int = 720,
    ) -> None:
        if (
            isinstance(max_height, bool)
            or not isinstance(max_height, int)
            or max_height <= 0
        ):
            raise ValueError("max_height must be a positive integer")
        self._videos = videos
        self._artifacts = artifacts
        self._max_height = max_height

    def backfill(
        self,
        *,
        limit: int,
        dry_run: bool,
        tenant_id: str | None = None,
        series_id: str | None = None,
        video_id: str | None = None,
    ) -> KeyframeBackfillReport:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        ready_count = 0
        backfilled_count = 0
        missing_media_count = 0
        concurrent_skip_count = 0
        failed_count = 0
        candidate_count = 0
        issues: list[KeyframeBackfillIssue] = []
        after = None
        scan_page_size = 256
        finished = False

        while not finished:
            candidates = self._videos.list_keyframe_backfill_candidates(
                limit=scan_page_size,
                tenant_id=tenant_id,
                series_id=series_id,
                video_id=video_id,
                after=after,
            )
            if not candidates:
                break
            after = candidates[-1]
            for candidate in candidates:
                if self._has_serviceable_timed_keyframe(candidate):
                    continue
                candidate_count += 1
                try:
                    canonical_path = self._artifacts.resolve(
                        candidate.chunk_media_ref
                    )
                except (FileNotFoundError, ValueError):
                    missing_media_count += 1
                    issues.append(KeyframeBackfillIssue(
                        video_chunk_id=candidate.video_chunk_id,
                        code="MEDIA_MISSING",
                    ))
                    if dry_run and candidate_count >= limit:
                        finished = True
                        break
                    continue
                except OSError as exc:
                    failed_count += 1
                    issues.append(KeyframeBackfillIssue(
                        video_chunk_id=candidate.video_chunk_id,
                        code="MEDIA_READ_FAILED",
                    ))
                    _logger.error(
                        "keyframe_backfill_media_read_failed "
                        "video_chunk_id=%s error_type=%s",
                        candidate.video_chunk_id,
                        type(exc).__name__,
                    )
                    if dry_run and candidate_count >= limit:
                        finished = True
                        break
                    continue
                ready_count += 1
                if dry_run:
                    if candidate_count >= limit:
                        finished = True
                        break
                    continue

                try:
                    with tempfile.TemporaryDirectory(
                        prefix="anivag-keyframe-backfill-"
                    ) as temporary_directory:
                        frames = extract_video_frames(
                            canonical_path,
                            [0],
                            output_dir=temporary_directory,
                            max_height=self._max_height,
                        )
                        if len(frames) != 1 or frames[0][0] != 0:
                            raise RuntimeError(
                                "canonical first-frame extraction returned an invalid result"
                            )
                        frame_path = Path(frames[0][1])
                        with frame_path.open("rb") as source:
                            checksum = hashlib.file_digest(source, "sha256").hexdigest()
                        artifact = self._artifacts.publish_video_artifact(
                            frame_path,
                            series_id=candidate.series_id,
                            video_id=candidate.video_id,
                            category="keyframes",
                            filename=(
                                f"backfill-{candidate.video_chunk_id}-{checksum}.jpg"
                            ),
                        )
                    changed = self._videos.set_keyframe_if_missing(
                        candidate,
                        VideoChunkKeyframeReference(
                            uri=artifact.uri,
                            at_seconds=candidate.start_seconds,
                        ),
                    )
                except Exception as exc:
                    failed_count += 1
                    issues.append(KeyframeBackfillIssue(
                        video_chunk_id=candidate.video_chunk_id,
                        code="BACKFILL_FAILED",
                    ))
                    _logger.error(
                        "keyframe_backfill_failed video_chunk_id=%s error_type=%s",
                        candidate.video_chunk_id,
                        type(exc).__name__,
                    )
                    continue

                if changed:
                    backfilled_count += 1
                    if backfilled_count >= limit:
                        finished = True
                        break
                else:
                    concurrent_skip_count += 1
                    issues.append(KeyframeBackfillIssue(
                        video_chunk_id=candidate.video_chunk_id,
                        code="CONCURRENT_UPDATE",
                    ))

            if len(candidates) < scan_page_size:
                break

        return KeyframeBackfillReport(
            dry_run=dry_run,
            limit=limit,
            candidate_count=candidate_count,
            ready_count=ready_count,
            backfilled_count=backfilled_count,
            missing_media_count=missing_media_count,
            concurrent_skip_count=concurrent_skip_count,
            failed_count=failed_count,
            issues=issues,
        )

    def _has_serviceable_timed_keyframe(
        self,
        candidate: KeyframeBackfillCandidate,
    ) -> bool:
        for value in candidate.keyframe_snapshot:
            uri = stored_keyframe_uri(value)
            at_seconds = stored_keyframe_at_seconds(value)
            if (
                uri is None
                or at_seconds is None
                or not candidate.start_seconds <= at_seconds < candidate.end_seconds
                or at_seconds >= candidate.duration_seconds
            ):
                continue
            try:
                path = self._artifacts.resolve(uri)
                if image_media_type(path) is not None:
                    return True
            except (OSError, ValueError):
                continue
        return False
