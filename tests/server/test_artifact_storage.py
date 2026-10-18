from __future__ import annotations

import hashlib
import json
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


def test_voiceprint_artifact_is_immutable_scoped_to_video_and_deleted_with_video(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    series_id, video_id, task_id = (str(uuid4()) for _ in range(3))
    source = tmp_path / "scores.json"
    source.write_bytes(b'{"lines":[],"samples":[]}')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    options = dict(series_id=series_id, video_id=video_id, category="voiceprints",
                   filename=f"{task_id}-{digest}.json")
    artifact = store.publish_video_artifact(source, **options)
    assert artifact.sha256 == digest
    assert f"/videos/{video_id}/voiceprints/{task_id}-" in artifact.uri
    assert store.publish_video_artifact(source, **options) == artifact
    source.write_bytes(b"changed")
    with pytest.raises(ArtifactIntegrityError, match="different bytes"):
        store.publish_video_artifact(source, **options)
    store.delete_video_artifacts(series_id, video_id)
    with pytest.raises(FileNotFoundError):
        store.resolve(artifact.uri)


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


def test_conversation_attachment_keeps_original_and_manifest_across_store_restart(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    conversation_id = str(uuid4())
    stored = store.put_conversation_attachment(
        conversation_id, b"original", extension="png", media_type="image/png", width=20, height=10,
    )
    restarted = LocalArtifactStore(store.root, environment="test")
    assert restarted.read_conversation_attachment(conversation_id, stored.uri) == stored
    assert restarted.resolve(stored.uri).read_bytes() == b"original"
    assert restarted.list_attachment_conversations() == (conversation_id,)
    assert not list(restarted.resolve(stored.uri).parent.glob("model*"))
    assert restarted.put_conversation_attachment(
        conversation_id, b"original", extension="png", media_type="image/png", width=20, height=10,
    ) == stored
    restarted.delete_conversation_artifacts(conversation_id)
    restarted.delete_conversation_artifacts(conversation_id)
    assert restarted.list_attachment_conversations() == ()


def test_conversation_attachment_rejects_cross_scope_and_noncanonical_uris(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    conversation_id = str(uuid4())
    stored = store.put_conversation_attachment(
        conversation_id, b"original", extension="jpg", media_type="image/jpeg", width=20, height=10,
    )
    for uri in (
        stored.uri.replace("artifact://test/", "artifact://development/"),
        stored.uri + "?ignored=true", stored.uri + "#fragment",
        stored.uri.replace("/original.jpg", "/./original.jpg"),
        stored.uri.replace("/original.jpg", "/%6friginal.jpg"),
        stored.uri.replace("/original.jpg", "/manifest.json"),
        "file:///etc/passwd", "https://example.org/image.jpg",
    ):
        with pytest.raises(ValueError, match="does not belong"):
            store.read_conversation_attachment(conversation_id, uri)
    with pytest.raises(ValueError, match="does not belong"):
        store.read_conversation_attachment(str(uuid4()), stored.uri)


def test_conversation_attachment_manifest_is_the_atomic_publication_marker(tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    conversation_id = str(uuid4())
    write = store._write_immutable

    def interrupted_write(path, data):
        if path.name == "manifest.json":
            raise OSError("interrupted before publication")
        write(path, data)

    monkeypatch.setattr(store, "_write_immutable", interrupted_write)
    with pytest.raises(OSError, match="interrupted"):
        store.put_conversation_attachment(
            conversation_id, b"original", extension="png", media_type="image/png", width=20, height=10,
        )
    original = next(store.base_directory.rglob("original.png"))
    with pytest.raises(FileNotFoundError):
        store.read_conversation_attachment(conversation_id, store.uri_for_path(original))
    monkeypatch.setattr(store, "_write_immutable", write)
    stored = store.put_conversation_attachment(
        conversation_id, b"original", extension="png", media_type="image/png", width=20, height=10,
    )
    assert store.read_conversation_attachment(conversation_id, stored.uri) == stored


def test_conversation_attachment_rejects_changed_bytes_and_manifest(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    conversation_id = str(uuid4())
    stored = store.put_conversation_attachment(
        conversation_id, b"original", extension="png", media_type="image/png", width=20, height=10,
    )
    original = store.resolve(stored.uri)
    manifest = original.with_name("manifest.json")
    metadata = json.loads(manifest.read_bytes())
    metadata["sizeBytes"] = True
    manifest.write_text(json.dumps(metadata))
    with pytest.raises(ArtifactIntegrityError, match="manifest"):
        store.read_conversation_attachment(conversation_id, stored.uri)
    with pytest.raises(ArtifactIntegrityError):
        store.put_conversation_attachment(
            conversation_id, b"original", extension="png", media_type="image/png", width=20, height=10,
        )
    original.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.put_conversation_attachment(
            conversation_id, b"original", extension="png", media_type="image/png", width=20, height=10,
        )


def test_attachment_symlink_cannot_alias_another_conversation_or_leave_store(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    conversation_id = str(uuid4())
    stored = store.put_conversation_attachment(
        conversation_id, b"original", extension="png", media_type="image/png", width=20, height=10,
    )
    original = store.resolve(stored.uri)
    other = store.base_directory / "other.png"
    other.write_bytes(b"private")
    original.unlink()
    original.symlink_to(other)
    with pytest.raises(ValueError, match="symbolic links"):
        store.read_conversation_attachment(conversation_id, stored.uri)
    with pytest.raises(ValueError, match="symbolic links"):
        store.put_conversation_attachment(
            conversation_id, b"original", extension="png", media_type="image/png", width=20, height=10,
        )
