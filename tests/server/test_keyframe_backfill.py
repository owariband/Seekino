from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import select, update

from server.cli import _build_parser
from server.model.dto.keyframeDTO import KeyframeBackfillCandidate
from server.model.video import VideoChunk
from server.schema import VideoChunkKeyframeReference
from server.service.keyframe_backfill import KeyframeBackfillService
from server.storage.artifact import LocalArtifactStore


def test_backfill_cli_exposes_safe_scope_dry_run_and_limit() -> None:
    args = _build_parser().parse_args([
        "backfill-keyframes",
        "--database-url",
        "sqlite+pysqlite:///:memory:",
        "--environment",
        "test",
        "--artifact-root",
        "/tmp/anivag-backfill-test",
        "--workspace-id",
        "workspace",
        "--series-id",
        "11111111-1111-4111-8111-111111111111",
        "--video-id",
        "22222222-2222-4222-8222-222222222222",
        "--limit",
        "25",
        "--max-height",
        "480",
        "--dry-run",
    ])

    assert args.command == "backfill-keyframes"
    assert args.workspace_id == "workspace"
    assert args.limit == 25
    assert args.max_height == 480
    assert args.dry_run is True
    assert args.apply is False
    with pytest.raises(SystemExit):
        _build_parser().parse_args([
            "backfill-keyframes",
            "--database-url",
            "sqlite+pysqlite:///:memory:",
            "--limit",
            "0",
            "--dry-run",
        ])
    with pytest.raises(SystemExit):
        _build_parser().parse_args([
            "backfill-keyframes",
            "--database-url",
            "sqlite+pysqlite:///:memory:",
        ])


def test_repository_pages_snapshots_and_does_not_overwrite_concurrent_parse(
    prepared_import,
    repository,
) -> None:
    engine, videos = repository
    report = videos.import_video_parse(prepared_import)
    second_id = report.video_chunk_ids[1]
    with engine.begin() as connection:
        connection.execute(
            update(VideoChunk)
            .where(
                VideoChunk.video_chunk_id == report.video_chunk_ids[0]
            )
            .values(keyframe_uris=["artifact://test/legacy-untimed.jpg"])
        )
        connection.execute(
            update(VideoChunk)
            .where(VideoChunk.video_chunk_id == second_id)
            .values(keyframe_uris=[{
                "uri": "artifact://test/current.jpg",
                "at_seconds": 10,
            }])
        )

    first_page = videos.list_keyframe_backfill_candidates(
        limit=1,
        workspace_id=prepared_import.workspace_id,
        series_id=prepared_import.series_id,
        video_id=prepared_import.video_id,
    )
    second_page = videos.list_keyframe_backfill_candidates(
        limit=1,
        workspace_id=prepared_import.workspace_id,
        series_id=prepared_import.series_id,
        video_id=prepared_import.video_id,
        after=first_page[0],
    )

    assert [item.video_chunk_id for item in [*first_page, *second_page]] == [
        report.video_chunk_ids[0],
        report.video_chunk_ids[1],
    ]
    candidate = first_page[0]
    assert candidate.keyframe_snapshot == (
        "artifact://test/legacy-untimed.jpg",
    )
    with engine.begin() as connection:
        connection.execute(
            update(VideoChunk)
            .where(VideoChunk.video_chunk_id == candidate.video_chunk_id)
            .values(keyframe_uris=[{
                "uri": "artifact://test/concurrent.jpg",
                "at_seconds": 0,
            }])
        )

    changed = videos.set_keyframe_if_missing(
        candidate,
        VideoChunkKeyframeReference(
            uri="artifact://test/backfill.jpg",
            at_seconds=0,
        ),
    )

    assert changed is False
    with engine.connect() as connection:
        stored = connection.execute(
            select(VideoChunk.keyframe_uris).where(
                VideoChunk.video_chunk_id == candidate.video_chunk_id
            )
        ).scalar_one()
    assert stored == [{
        "uri": "artifact://test/concurrent.jpg",
        "at_seconds": 0,
    }]
    assert videos.list_keyframe_backfill_candidates(
        limit=10,
        workspace_id="other-workspace",
    ) == []
    with pytest.raises(ValueError, match="positive integer"):
        videos.list_keyframe_backfill_candidates(limit=0)


def test_repository_backfill_prepends_frame_and_preserves_historical_uris(
    prepared_import,
    repository,
) -> None:
    engine, videos = repository
    report = videos.import_video_parse(prepared_import)
    video_chunk_id = report.video_chunk_ids[0]
    legacy = "artifact://test/keyframes/legacy.jpg"
    historical = {
        "uri": "artifact://test/keyframes/historical.jpg",
        "at_seconds": None,
    }
    with engine.begin() as connection:
        connection.execute(
            update(VideoChunk)
            .where(VideoChunk.video_chunk_id == video_chunk_id)
            .values(keyframe_uris=[legacy, historical])
        )

    [candidate] = videos.list_keyframe_backfill_candidates(limit=1)
    current = {
        "uri": "artifact://test/keyframes/current.jpg",
        "at_seconds": candidate.start_seconds,
    }

    assert videos.set_keyframe_if_missing(
        candidate,
        VideoChunkKeyframeReference.model_validate(current),
    ) is True
    with engine.connect() as connection:
        stored = connection.execute(
            select(VideoChunk.keyframe_uris).where(
                VideoChunk.video_chunk_id == video_chunk_id
            )
        ).scalar_one()

    assert stored == [current, legacy, historical]


def test_backfill_is_dry_run_bounded_resumable_and_uses_absolute_time(
    prepared_import,
    repository,
    tmp_path,
    monkeypatch,
) -> None:
    engine, videos = repository
    videos.import_video_parse(prepared_import)
    artifacts = LocalArtifactStore(
        tmp_path / "artifacts",
        environment="test",
    )
    with engine.begin() as connection:
        chunks = connection.execute(
            select(
                VideoChunk.video_chunk_id,
                VideoChunk.sequence_no,
            ).order_by(VideoChunk.sequence_no)
        ).all()
        for video_chunk_id, sequence_no in chunks:
            canonical = (
                artifacts.base_directory
                / "fixtures"
                / f"chunk-{sequence_no:04d}.mp4"
            )
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_bytes(f"canonical-{sequence_no}".encode())
            connection.execute(
                update(VideoChunk)
                .where(VideoChunk.video_chunk_id == video_chunk_id)
                .values(chunk_media_ref=artifacts.uri_for_path(canonical))
            )

    extracted: list[Path] = []

    def extract_first_frame(video_path, timestamps_ms, *, output_dir, max_height):
        assert timestamps_ms == [0]
        assert max_height == 360
        output = Path(output_dir) / "frame-000-00000000ms.jpg"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"\xff\xd8\xff" + Path(video_path).read_bytes())
        extracted.append(Path(video_path))
        return [(0, output)]

    monkeypatch.setattr(
        "server.service.keyframe_backfill.extract_video_frames",
        extract_first_frame,
    )
    service = KeyframeBackfillService(videos, artifacts, max_height=360)

    dry_run = service.backfill(
        limit=1,
        dry_run=True,
        workspace_id=prepared_import.workspace_id,
    )
    assert dry_run.candidate_count == dry_run.ready_count == 1
    assert dry_run.backfilled_count == 0
    assert extracted == []

    first = service.backfill(
        limit=1,
        dry_run=False,
        workspace_id=prepared_import.workspace_id,
    )
    second = service.backfill(
        limit=1,
        dry_run=False,
        workspace_id=prepared_import.workspace_id,
    )
    complete = service.backfill(
        limit=1,
        dry_run=False,
        workspace_id=prepared_import.workspace_id,
    )

    assert first.backfilled_count == second.backfilled_count == 1
    assert complete.candidate_count == 0
    assert len(extracted) == 2
    with engine.connect() as connection:
        stored = connection.execute(
            select(
                VideoChunk.start_seconds,
                VideoChunk.keyframe_uris,
            ).order_by(VideoChunk.sequence_no)
        ).all()
    assert [values[0]["at_seconds"] for _, values in stored] == [0, 10]
    for _start_seconds, values in stored:
        assert "/keyframes/" in values[0]["uri"]
        assert artifacts.resolve(values[0]["uri"]).read_bytes().startswith(
            b"\xff\xd8\xff"
        )


def test_backfill_failure_does_not_consume_apply_limit_or_starve_later_chunk(
    prepared_import,
    repository,
    tmp_path,
    monkeypatch,
) -> None:
    engine, videos = repository
    report = videos.import_video_parse(prepared_import)
    artifacts = LocalArtifactStore(
        tmp_path / "artifacts",
        environment="test",
    )
    canonical = artifacts.base_directory / "fixtures" / "chunk-0001.mp4"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"canonical-1")
    with engine.begin() as connection:
        connection.execute(
            update(VideoChunk)
            .where(
                VideoChunk.video_chunk_id == report.video_chunk_ids[1]
            )
            .values(chunk_media_ref=artifacts.uri_for_path(canonical))
        )

    extracted: list[Path] = []

    def extract_later_frame(video_path, timestamps_ms, *, output_dir, **_kwargs):
        assert Path(video_path) == canonical
        output = Path(output_dir) / "frame.jpg"
        output.write_bytes(b"\xff\xd8\xfflater")
        extracted.append(Path(video_path))
        return [(timestamps_ms[0], output)]

    monkeypatch.setattr(
        "server.service.keyframe_backfill.extract_video_frames",
        extract_later_frame,
    )

    result = KeyframeBackfillService(videos, artifacts).backfill(
        limit=1,
        dry_run=False,
    )

    assert result.candidate_count == 2
    assert result.ready_count == 1
    assert result.backfilled_count == 1
    assert result.missing_media_count == 1
    assert result.issues[0].code == "MEDIA_MISSING"
    assert extracted == [canonical]


def test_backfill_replaces_broken_timed_frame_and_skips_serviceable_frame(
    prepared_import,
    repository,
    tmp_path,
    monkeypatch,
) -> None:
    engine, videos = repository
    report = videos.import_video_parse(prepared_import)
    artifacts = LocalArtifactStore(
        tmp_path / "artifacts",
        environment="test",
    )
    existing_frame = artifacts.base_directory / "keyframes" / "existing.jpg"
    existing_frame.parent.mkdir(parents=True, exist_ok=True)
    existing_frame.write_bytes(b"\xff\xd8\xffexisting")
    canonical = artifacts.base_directory / "fixtures" / "chunk-0001.mp4"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"canonical-1")
    broken_uri = "artifact://test/keyframes/deleted.jpg"
    with engine.begin() as connection:
        connection.execute(
            update(VideoChunk)
            .where(
                VideoChunk.video_chunk_id == report.video_chunk_ids[0]
            )
            .values(keyframe_uris=[{
                "uri": artifacts.uri_for_path(existing_frame),
                "at_seconds": 0,
            }])
        )
        connection.execute(
            update(VideoChunk)
            .where(
                VideoChunk.video_chunk_id == report.video_chunk_ids[1]
            )
            .values(
                chunk_media_ref=artifacts.uri_for_path(canonical),
                keyframe_uris=[{"uri": broken_uri, "at_seconds": 10}],
            )
        )

    def extract_replacement(_video_path, timestamps_ms, *, output_dir, **_kwargs):
        output = Path(output_dir) / "replacement.jpg"
        output.write_bytes(b"\xff\xd8\xffreplacement")
        return [(timestamps_ms[0], output)]

    monkeypatch.setattr(
        "server.service.keyframe_backfill.extract_video_frames",
        extract_replacement,
    )

    result = KeyframeBackfillService(videos, artifacts).backfill(
        limit=1,
        dry_run=False,
    )

    assert result.candidate_count == result.ready_count == 1
    assert result.backfilled_count == 1
    with engine.connect() as connection:
        stored = connection.execute(
            select(VideoChunk.keyframe_uris).where(
                VideoChunk.video_chunk_id == report.video_chunk_ids[1]
            )
        ).scalar_one()
    assert len(stored) == 1
    assert stored[0]["at_seconds"] == 10
    assert stored[0]["uri"] != broken_uri


def test_backfill_failure_log_does_not_expose_media_path(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    candidate = KeyframeBackfillCandidate(
        workspace_id="workspace",
        series_id="11111111-1111-4111-8111-111111111111",
        video_id="22222222-2222-4222-8222-222222222222",
        episode_order=1,
        duration_seconds=10,
        video_chunk_id="33333333-3333-4333-8333-333333333333",
        sequence_no=0,
        start_seconds=0,
        end_seconds=10,
        chunk_media_ref="artifact://test/private/chunk.mp4",
        keyframe_snapshot=(),
    )

    class Videos:
        def list_keyframe_backfill_candidates(self, **_kwargs):
            return [candidate]

    class Artifacts:
        def resolve(self, _uri):
            return tmp_path / "private" / "chunk.mp4"

    private_path = "/private/sensitive/canonical-chunk.mp4"

    def fail_extract(*_args, **_kwargs):
        raise RuntimeError(f"ffmpeg failed for {private_path}")

    monkeypatch.setattr(
        "server.service.keyframe_backfill.extract_video_frames",
        fail_extract,
    )
    caplog.set_level(logging.ERROR, logger="server.service.keyframe_backfill")

    report = KeyframeBackfillService(
        Videos(),  # type: ignore[arg-type]
        Artifacts(),  # type: ignore[arg-type]
    ).backfill(limit=1, dry_run=False)

    assert report.failed_count == 1
    assert "error_type=RuntimeError" in caplog.text
    assert private_path not in caplog.text
