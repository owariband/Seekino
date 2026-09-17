from __future__ import annotations

import hashlib
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from server.config.media import ImageUploadConfig
from server.database import DatabaseEnvironment
from server.storage.artifact import (
    ArtifactIntegrityError,
    LocalArtifactStore,
    UploadSegmentArtifact,
)
from server.utils.images import prepare_series_image
from server.utils.media import decode_series_image_id


def test_local_artifact_store_can_open_without_creating_directories(tmp_path) -> None:
    root = tmp_path / "read-only-artifacts"

    store = LocalArtifactStore(
        root,
        environment="test",
        create_base_directory=False,
    )

    assert store.base_directory == root / "test"
    assert not root.exists()


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


def test_series_images_store_original_and_immutable_model_scoped_by_hash(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    series_id = str(uuid4())
    output = BytesIO()
    Image.new("RGBA", (32, 24), (1, 2, 3, 128)).save(output, format="PNG")
    data = output.getvalue()
    extension, model_data = prepare_series_image(data, config=ImageUploadConfig())

    image_id = store.put_series_image(series_id, data, extension=extension, model_data=model_data)
    assert decode_series_image_id(image_id) == (series_id, hashlib.sha256(data).hexdigest(), "png")
    original = store.resolve_series_image(image_id, series_id=series_id)
    model = store.resolve_series_image(image_id, series_id=series_id, variant="model")
    assert original.read_bytes() == data
    assert model.read_bytes() == model_data
    assert str(original).startswith(str(store.base_directory / "media" / "series" / series_id))

    assert store.put_series_image(series_id, data, extension=extension, model_data=model_data) == image_id
    _extension, smaller_model = prepare_series_image(data, config=ImageUploadConfig(model_max_edge=8))
    assert store.put_series_image(series_id, data, extension=extension, model_data=smaller_model) == image_id
    assert model.read_bytes() == model_data
    with pytest.raises(ValueError, match="another Series"):
        store.resolve_series_image(image_id, series_id=str(uuid4()))
    with pytest.raises(ValueError, match="unsupported Series image variant"):
        store.resolve_series_image(image_id, series_id=series_id, variant="../../private")
    with pytest.raises(FileNotFoundError):
        LocalArtifactStore(tmp_path / "artifacts", environment="development").resolve_series_image(
            image_id, series_id=series_id,
        )

    original.write_bytes(b"corrupt")
    with pytest.raises(ArtifactIntegrityError, match="stored Series image bytes changed"):
        store.put_series_image(series_id, data, extension=extension, model_data=model_data)


def test_series_image_resolve_rejects_filesystem_escape(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    series_id = str(uuid4())
    image_id = store.put_series_image(series_id, b"original", extension="jpg", model_data=b"model")
    original = store.resolve_series_image(image_id, series_id=series_id)
    outside = tmp_path / "private.jpg"
    outside.write_bytes(b"private")
    original.unlink()
    original.symlink_to(outside)

    with pytest.raises(ValueError, match="escapes the configured root"):
        store.resolve_series_image(image_id, series_id=series_id)
    with pytest.raises(ValueError, match="escapes the configured root"):
        store.put_series_image(series_id, b"original", extension="jpg", model_data=b"model")
