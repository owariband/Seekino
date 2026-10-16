from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from urllib.parse import urlencode

from ..config.media import ImageUploadConfig
from ..constants.identity import LOCAL_USER_ID
from ..model.dto.agentDTO import AgentAttachmentState
from ..model.dto.mediaDTO import VideoMedia
from ..repository.agent import AgentConflictError, AgentRepository
from ..storage.artifact import ArtifactIntegrityError, ArtifactStore, StoredImageAttachment
from ..utils.images import inspect_static_image
from ..utils.validation import canonical_uuid
from .media import MediaMissingError

_logger = logging.getLogger(__name__)


class AgentAttachmentService:
    def __init__(
        self, repository: AgentRepository, artifact_store: ArtifactStore,
        *, config: ImageUploadConfig,
    ) -> None:
        self._repository = repository
        self._artifacts = artifact_store
        self.image_upload_config = config

    def upload(
        self, conversation_id: str, data: bytes, *, tenant_id: str,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentAttachmentState:
        conversation_id = canonical_uuid(conversation_id, "conversation_id")
        self._repository.get_conversation(
            conversation_id, tenant_id, owner_user_id=owner_user_id,
        )
        info = inspect_static_image(data, config=self.image_upload_config)
        # Decode before locking. Publication and SQL conversation deletion use
        # the same row lock, so a late upload cannot recreate a deleted scope.
        with self._repository.lock_conversation(
            conversation_id, tenant_id, owner_user_id=owner_user_id,
        ):
            stored = self._artifacts.put_conversation_attachment(
                conversation_id, data, extension=info.extension,
                media_type=info.media_type, width=info.width, height=info.height,
            )
        return self._state(conversation_id, tenant_id, stored)

    def describe(
        self, conversation_id: str, media_uri: str, *, tenant_id: str,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentAttachmentState:
        conversation_id = canonical_uuid(conversation_id, "conversation_id")
        self._repository.get_conversation(
            conversation_id, tenant_id, owner_user_id=owner_user_id,
        )
        stored = self._read_manifest(conversation_id, media_uri)
        return self._state(conversation_id, tenant_id, stored)

    def get_content(
        self, conversation_id: str, media_uri: str, *, tenant_id: str,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> VideoMedia:
        conversation_id = canonical_uuid(conversation_id, "conversation_id")
        self._repository.get_conversation(
            conversation_id, tenant_id, owner_user_id=owner_user_id,
        )
        stored = self._read_manifest(conversation_id, media_uri)
        self._verify_original(stored)
        return VideoMedia(
            self._artifacts.resolve(stored.uri), stored.media_type, stored.size_bytes,
        )

    def canonicalize_references(
        self, conversation_id: str, refs: Sequence[Mapping[str, str]], *, tenant_id: str,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> list[dict[str, str]]:
        conversation_id = canonical_uuid(conversation_id, "conversation_id")
        self._repository.get_conversation(
            conversation_id, tenant_id, owner_user_id=owner_user_id,
        )
        result = []
        for ref in refs:
            if not isinstance(ref, Mapping) or set(ref) != {"mediaUri"}:
                raise ValueError("attachment must contain only mediaUri")
            stored = self._read_manifest(conversation_id, ref["mediaUri"])
            self._verify_original(stored)
            result.append({"mediaUri": stored.uri})
        return result

    def delete_conversation_artifacts(self, conversation_id: str) -> None:
        conversation_id = canonical_uuid(conversation_id, "conversation_id")
        if self._repository.conversation_exists(conversation_id):
            raise AgentConflictError("attachments are retained while their conversation exists")
        self._artifacts.delete_conversation_artifacts(conversation_id)

    def reconcile_deletions(self) -> int:
        removed = 0
        for conversation_id in self._artifacts.list_attachment_conversations():
            try:
                if self._repository.conversation_exists(conversation_id):
                    continue
                self._artifacts.delete_conversation_artifacts(conversation_id)
                removed += 1
            except Exception:
                # A failed DB lookup is not evidence that the conversation was
                # deleted. Keep the bytes and retry on a later startup.
                _logger.warning(
                    "agent_attachment_cleanup_failed conversation_id=%s",
                    conversation_id, exc_info=True,
                )
        return removed

    def _read_manifest(self, conversation_id: str, media_uri: str) -> StoredImageAttachment:
        try:
            return self._artifacts.read_conversation_attachment(conversation_id, media_uri)
        except FileNotFoundError as exc:
            raise MediaMissingError("附件元数据文件已丢失") from exc

    def _verify_original(self, stored: StoredImageAttachment) -> None:
        try:
            actual = self._artifacts.fingerprint(stored.uri)
        except FileNotFoundError as exc:
            raise MediaMissingError("附件原始文件已丢失") from exc
        if actual.sha256 != stored.sha256 or actual.size_bytes != stored.size_bytes:
            raise ArtifactIntegrityError("stored attachment bytes disagree with their manifest")

    @staticmethod
    def _state(
        conversation_id: str, tenant_id: str, stored: StoredImageAttachment,
    ) -> AgentAttachmentState:
        return AgentAttachmentState(
            media_uri=stored.uri, media_type=stored.media_type, size_bytes=stored.size_bytes,
            preview_url="/api/agent/attachments/content?" + urlencode({
                "tenantId": tenant_id, "conversationId": conversation_id, "mediaUri": stored.uri,
            }),
        )
