from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from server.config.media import ImageUploadConfig
from server.repository.agent import AgentConflictError, AgentConversationNotFoundError
from server.service.agent_attachments import AgentAttachmentService
from server.service.media import MediaMissingError
from server.storage.artifact import ArtifactIntegrityError, LocalArtifactStore


class ConversationRepository:
    def __init__(self):
        self.records = {}
        self.locked = False
        self.before_lock = None

    def add(self, *, tenant="workspace", owner="user"):
        identifier = str(uuid4())
        self.records[identifier] = SimpleNamespace(tenant_id=tenant, owner_user_id=owner)
        return identifier

    def get_conversation(self, conversation_id, tenant_id, *, owner_user_id):
        record = self.records.get(conversation_id)
        if record is None or (record.tenant_id, record.owner_user_id) != (tenant_id, owner_user_id):
            raise AgentConversationNotFoundError("conversation not found")
        return record

    @contextmanager
    def lock_conversation(self, conversation_id, tenant_id, *, owner_user_id):
        if self.before_lock:
            self.before_lock()
        record = self.get_conversation(conversation_id, tenant_id, owner_user_id=owner_user_id)
        self.locked = True
        try:
            yield record
        finally:
            self.locked = False

    def conversation_exists(self, conversation_id):
        return conversation_id in self.records


@pytest.fixture
def attachments(tmp_path):
    repository = ConversationRepository()
    conversation_id = repository.add()
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    config = ImageUploadConfig(model_max_edge=1)
    service = AgentAttachmentService(repository, store, config=config)
    return service, repository, store, conversation_id


def image_bytes(color="red"):
    output = BytesIO()
    Image.new("RGBA", (40, 30), color).save(output, format="PNG")
    return output.getvalue()


def upload(service, conversation_id, data=None):
    return service.upload(
        conversation_id, image_bytes() if data is None else data,
        tenant_id="workspace", owner_user_id="user",
    )


def test_upload_preserves_original_and_recovers_durable_metadata(attachments):
    service, repository, store, conversation_id = attachments
    data = image_bytes()
    state = upload(service, conversation_id, data)
    restarted = AgentAttachmentService(
        repository, LocalArtifactStore(store.root, environment="test"),
        config=service.image_upload_config,
    )
    assert restarted.image_upload_config is service.image_upload_config
    assert restarted.describe(
        conversation_id, state.media_uri, tenant_id="workspace", owner_user_id="user",
    ) == state
    media = restarted.get_content(
        conversation_id, state.media_uri, tenant_id="workspace", owner_user_id="user",
    )
    assert (media.media_type, media.size_bytes, media.path.read_bytes()) == ("image/png", len(data), data)
    assert "tenantId=workspace" in state.preview_url
    assert f"conversationId={conversation_id}" in state.preview_url
    with Image.open(media.path) as image:
        assert image.size == (40, 30)
        assert image.mode == "RGBA"
    assert upload(restarted, conversation_id, data) == state


@pytest.mark.parametrize("tenant,owner", [("other", "user"), ("workspace", "another-user")])
def test_upload_read_and_reference_check_conversation_owner_and_tenant(attachments, tenant, owner):
    service, _repository, _store, conversation_id = attachments
    state = upload(service, conversation_id)
    with pytest.raises(AgentConversationNotFoundError):
        service.upload(conversation_id, b"not decoded", tenant_id=tenant, owner_user_id=owner)
    for method in (service.describe, service.get_content):
        with pytest.raises(AgentConversationNotFoundError):
            method(conversation_id, state.media_uri, tenant_id=tenant, owner_user_id=owner)
    with pytest.raises(AgentConversationNotFoundError):
        service.canonicalize_references(
            conversation_id, [{"mediaUri": state.media_uri}], tenant_id=tenant, owner_user_id=owner,
        )


def test_references_preserve_order_and_reject_cross_conversation_or_extra_fields(attachments):
    service, repository, _store, conversation_id = attachments
    first = upload(service, conversation_id)
    second = upload(service, conversation_id, image_bytes("blue"))
    references = [{"mediaUri": second.media_uri}, {"mediaUri": first.media_uri}]
    assert service.canonicalize_references(
        conversation_id, references, tenant_id="workspace", owner_user_id="user",
    ) == references
    other = repository.add()
    with pytest.raises(ValueError, match="does not belong"):
        service.canonicalize_references(
            other, references, tenant_id="workspace", owner_user_id="user",
        )
    with pytest.raises(ValueError, match="only mediaUri"):
        service.canonicalize_references(
            conversation_id, [{"mediaUri": first.media_uri, "previewUrl": "forged"}],
            tenant_id="workspace", owner_user_id="user",
        )


def test_original_loss_does_not_hide_metadata_but_read_and_send_fail(attachments):
    service, _repository, store, conversation_id = attachments
    state = upload(service, conversation_id)
    store.resolve(state.media_uri).unlink()
    assert service.describe(
        conversation_id, state.media_uri, tenant_id="workspace", owner_user_id="user",
    ) == state
    with pytest.raises(MediaMissingError, match="原始"):
        service.get_content(conversation_id, state.media_uri, tenant_id="workspace", owner_user_id="user")
    with pytest.raises(MediaMissingError, match="原始"):
        service.canonicalize_references(
            conversation_id, [{"mediaUri": state.media_uri}],
            tenant_id="workspace", owner_user_id="user",
        )


def test_changed_original_cannot_be_read_or_sent(attachments):
    service, _repository, store, conversation_id = attachments
    state = upload(service, conversation_id)
    store.resolve(state.media_uri).write_bytes(b"changed")
    with pytest.raises(ArtifactIntegrityError, match="manifest"):
        service.get_content(conversation_id, state.media_uri, tenant_id="workspace", owner_user_id="user")
    with pytest.raises(ArtifactIntegrityError, match="manifest"):
        service.canonicalize_references(
            conversation_id, [{"mediaUri": state.media_uri}],
            tenant_id="workspace", owner_user_id="user",
        )


def test_publication_holds_lifecycle_lock_and_deleted_conversation_cannot_publish(attachments, monkeypatch):
    service, repository, store, conversation_id = attachments
    publish = store.put_conversation_attachment

    def checked_publish(*args, **kwargs):
        assert repository.locked
        return publish(*args, **kwargs)

    monkeypatch.setattr(store, "put_conversation_attachment", checked_publish)
    upload(service, conversation_id)
    late = repository.add()
    repository.before_lock = lambda: repository.records.pop(late)
    with pytest.raises(AgentConversationNotFoundError):
        upload(service, late)
    assert late not in store.list_attachment_conversations()


def test_unsubmitted_attachments_remain_until_conversation_deletion(attachments):
    service, repository, store, conversation_id = attachments
    state = upload(service, conversation_id)
    assert service.reconcile_deletions() == 0
    with pytest.raises(AgentConflictError, match="retained"):
        service.delete_conversation_artifacts(conversation_id)
    assert store.resolve(state.media_uri).is_file()
    repository.records.pop(conversation_id)
    with pytest.raises(AgentConversationNotFoundError):
        service.get_content(conversation_id, state.media_uri, tenant_id="workspace", owner_user_id="user")
    assert service.reconcile_deletions() == 1
    assert service.reconcile_deletions() == 0
    service.delete_conversation_artifacts(conversation_id)
    assert store.list_attachment_conversations() == ()


def test_cleanup_keeps_files_on_database_failure_and_retries_io_failure(attachments, monkeypatch, caplog):
    service, repository, store, conversation_id = attachments
    state = upload(service, conversation_id)
    exists = repository.conversation_exists

    def unavailable(_identifier):
        raise OSError("DB unavailable")

    monkeypatch.setattr(repository, "conversation_exists", unavailable)
    assert service.reconcile_deletions() == 0
    assert store.resolve(state.media_uri).is_file()
    assert "agent_attachment_cleanup_failed" in caplog.text
    monkeypatch.setattr(repository, "conversation_exists", exists)
    repository.records.pop(conversation_id)
    delete = store.delete_conversation_artifacts
    monkeypatch.setattr(store, "delete_conversation_artifacts", unavailable)
    assert service.reconcile_deletions() == 0
    assert store.resolve(state.media_uri).is_file()
    monkeypatch.setattr(store, "delete_conversation_artifacts", delete)
    assert service.reconcile_deletions() == 1
