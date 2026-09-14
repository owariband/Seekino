from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from server.database import DatabaseEnvironment
from server.storage.artifact import (
    ArtifactIntegrityError,
    LocalArtifactStore,
    UploadSegmentArtifact,
)


def test_local_artifact_store_assembles_and_publishes_upload(tmp_path) -> None:
    store = LocalArtifactStore(
        tmp_path / "artifacts",
        environment=DatabaseEnvironment.TEST,
    )
    upload_session_id = str(uuid4())
    series_id = str(uuid4())
    video_id = str(uuid4())
    first = store.put_upload_segment(upload_session_id, 0, b"first-")
    second = store.put_upload_segment(upload_session_id, 1, b"second")

    assembled = store.assemble_upload(
        upload_session_id,
        [
            UploadSegmentArtifact(
                segment_number=1,
                uri=second.uri,
                size_bytes=second.size_bytes,
                sha256=second.sha256,
            ),
            UploadSegmentArtifact(
                segment_number=0,
                uri=first.uri,
                size_bytes=first.size_bytes,
                sha256=first.sha256,
            ),
        ],
        expected_total_size=12,
    )

    assert assembled.sha256 == hashlib.sha256(b"first-second").hexdigest()
    assert store.resolve(assembled.uri).read_bytes() == b"first-second"

    published = store.publish_original(
        assembled,
        series_id=series_id,
        video_id=video_id,
        original_filename="第 7 集.mp4",
    )

    assert store.resolve(published.uri).read_bytes() == b"first-second"
    assert published.uri.startswith("artifact://test/media/series/")


def test_artifact_store_rejects_changed_segment_bytes(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    upload_session_id = str(uuid4())
    stored = store.put_upload_segment(upload_session_id, 0, b"original")
    store.resolve(stored.uri).write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError, match="checksum changed"):
        store.assemble_upload(
            upload_session_id,
            [
                UploadSegmentArtifact(
                    segment_number=0,
                    uri=stored.uri,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                )
            ],
            expected_total_size=stored.size_bytes,
        )


def test_artifact_store_enforces_environment_and_safe_filenames(tmp_path) -> None:
    test_store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    development_store = LocalArtifactStore(
        tmp_path / "artifacts",
        environment="development",
    )
    upload_session_id = str(uuid4())
    stored = test_store.put_upload_segment(upload_session_id, 0, b"data")

    with pytest.raises(ValueError, match="another store or environment"):
        development_store.resolve(stored.uri)
    with pytest.raises(ValueError, match="must not contain a path"):
        test_store.publish_original(
            stored,
            series_id=str(uuid4()),
            video_id=str(uuid4()),
            original_filename="../episode.mp4",
        )
