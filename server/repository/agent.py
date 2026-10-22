from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from agent.chat.common.state import AnswerPhase

from ..constants.identity import LOCAL_USER_ID
from ..model.agent import (
    AGENT_MESSAGE_IDEMPOTENCY_KEY,
    AgentConversation,
    AgentMessage,
    AgentMessageRole,
    AgentMessageStatus,
)
from ..model.dto.agentDTO import (
    AgentConversationState,
    AgentFailure,
    AgentMessageState,
    AgentMessageTimingState,
    AgentStageTimingState,
    StartedAgentMessageState,
)


class AgentConversationNotFoundError(LookupError):
    pass


class AgentMessageNotFoundError(LookupError):
    pass


class AgentConflictError(RuntimeError):
    pass


class AgentMessageInProgressError(RuntimeError):
    pass


class AgentReferenceInvalidError(ValueError):
    pass


class AgentRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_conversation(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        title: str,
        idempotency_key: str,
        title_is_auto: bool = False,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentConversationState:
        for attempt in range(2):
            try:
                with self._session_factory.begin() as session:
                    existing = session.scalar(
                        select(AgentConversation).where(
                            AgentConversation.tenant_id == tenant_id,
                            AgentConversation.owner_id == owner_user_id,
                            AgentConversation.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        if existing.title_is_auto != title_is_auto or (
                            not title_is_auto and existing.title != title
                        ):
                            raise AgentConflictError(
                                "the conversation idempotency key is already used "
                                "with different fields"
                            )
                        message_count = session.scalar(
                            select(func.count(AgentMessage.id)).where(
                                AgentMessage.conversation_id
                                == existing.id
                            )
                        )
                        return self._conversation_state(
                            existing,
                            message_count=int(message_count or 0),
                            last_message_preview=session.scalar(self._preview_query(existing.id)),
                        )

                    record = AgentConversation(
                        id=conversation_id,
                        tenant_id=tenant_id,
                        owner_id=owner_user_id,
                        title=title,
                        title_is_auto=title_is_auto,
                        idempotency_key=idempotency_key,
                        next_message_sequence=0,
                    )
                    session.add(record)
                    session.flush()
                    return self._conversation_state(
                        record,
                        message_count=0,
                    )
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    def list_conversations(
        self,
        tenant_id: str,
        *,
        limit: int,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> list[AgentConversationState]:
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    AgentConversation,
                    func.count(AgentMessage.id),
                    self._preview_query(AgentConversation.id).scalar_subquery(),
                )
                .outerjoin(
                    AgentMessage,
                    AgentMessage.conversation_id
                    == AgentConversation.id,
                )
                .where(AgentConversation.tenant_id == tenant_id, AgentConversation.owner_id == owner_user_id)
                .group_by(AgentConversation.id)
                .order_by(
                    AgentConversation.last_message_time.desc(),
                    AgentConversation.create_time.desc(),
                    AgentConversation.id.desc(),
                )
                .limit(limit)
            ).all()
            return [
                self._conversation_state(
                    record,
                    message_count=int(count),
                    last_message_preview=preview,
                )
                for record, count, preview in rows
            ]

    def get_conversation(
        self,
        conversation_id: str,
        tenant_id: str,
        *,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentConversationState:
        with self._session_factory() as session:
            record = self._require_conversation(
                session,
                conversation_id,
                tenant_id,
                owner_user_id=owner_user_id,
            )
            message_count = session.scalar(
                select(func.count(AgentMessage.id)).where(
                    AgentMessage.conversation_id == conversation_id
                )
            )
            return self._conversation_state(
                record,
                message_count=int(message_count or 0),
                last_message_preview=session.scalar(self._preview_query(conversation_id)),
            )

    @contextmanager
    def lock_conversation(
        self,
        conversation_id: str,
        tenant_id: str,
        *,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> Iterator[AgentConversationState]:
        """Serialize short attachment publication with conversation deletion."""
        with self._session_factory.begin() as session:
            record = self._require_conversation(
                session, conversation_id, tenant_id,
                for_update=True, owner_user_id=owner_user_id,
            )
            count = session.scalar(select(func.count(AgentMessage.id)).where(
                AgentMessage.conversation_id == conversation_id,
            ))
            yield self._conversation_state(
                record, message_count=int(count or 0),
                last_message_preview=session.scalar(self._preview_query(conversation_id)),
            )

    def conversation_exists(self, conversation_id: str) -> bool:
        with self._session_factory() as session:
            return session.scalar(select(AgentConversation.id).where(
                AgentConversation.id == conversation_id,
            )) is not None

    def has_message_attachments(
        self,
        conversation_id: str,
        tenant_id: str,
        *,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> bool:
        with self._session_factory() as session:
            self._require_conversation(
                session, conversation_id, tenant_id, owner_user_id=owner_user_id,
            )
            return any(session.scalars(select(AgentMessage.attachments).where(
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.role == AgentMessageRole.USER.value,
                AgentMessage.status == AgentMessageStatus.COMPLETED.value,
            )))

    def delete_conversation(
        self,
        conversation_id: str,
        tenant_id: str,
        *,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentConversationState:
        with self._session_factory.begin() as session:
            record = self._require_conversation(
                session,
                conversation_id,
                tenant_id,
                for_update=True,
                owner_user_id=owner_user_id,
            )
            # The Conversation row is the lifecycle mutex. Completion locks the
            # Message before this row, so deletion must not lock them in reverse.
            processing = session.scalar(
                select(AgentMessage.id.label("message_id"))
                .where(
                    AgentMessage.conversation_id == conversation_id,
                    AgentMessage.role == AgentMessageRole.ASSISTANT.value,
                    AgentMessage.status == AgentMessageStatus.PROCESSING.value,
                )
                .limit(1)
            )
            if processing is not None:
                raise AgentMessageInProgressError(
                    "this conversation has an assistant message in progress"
                )
            message_count = int(
                session.scalar(
                    select(func.count(AgentMessage.id)).where(
                        AgentMessage.conversation_id == conversation_id
                    )
                )
                or 0
            )
            state = self._conversation_state(
                record,
                message_count=message_count,
                last_message_preview=session.scalar(
                    self._preview_query(conversation_id)
                ),
            )
            session.delete(record)
            session.flush()
            return state

    def list_conversation_page(
        self, tenant_id: str, *, offset: int, limit: int,
        series_id: str | None = None, query: str | None = None,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> tuple[list[AgentConversationState], int]:
        filters = [AgentConversation.tenant_id == tenant_id,
                   AgentConversation.owner_id == owner_user_id]
        if query:
            filters.append(AgentConversation.title.contains(query, autoescape=True))
        if series_id:
            if self._session_factory.kw["bind"].dialect.name == "sqlite":
                values = func.json_each(AgentMessage.series_ids).table_valued("value")
                matching = select(values.c.value).where(values.c.value == series_id).exists()
            else:
                matching = func.json_contains(AgentMessage.series_ids, json.dumps(series_id)) == 1
            filters.append(select(AgentMessage.id.label("message_id")).where(
                AgentMessage.conversation_id == AgentConversation.id,
                matching,
            ).correlate(AgentConversation).exists())
        with self._session_factory() as session:
            total = int(session.scalar(select(func.count()).select_from(AgentConversation).where(*filters)) or 0)
            rows = session.execute(select(
                AgentConversation, func.count(AgentMessage.id),
                self._preview_query(AgentConversation.id).scalar_subquery(),
            ).outerjoin(AgentMessage,
                        AgentMessage.conversation_id == AgentConversation.id)
              .where(*filters).group_by(AgentConversation.id)
              .order_by(AgentConversation.update_time.desc(), AgentConversation.id.desc())
              .offset(offset).limit(limit)).all()
            return [self._conversation_state(record, message_count=int(count), last_message_preview=preview)
                    for record, count, preview in rows], total

    def list_message_page(
        self, conversation_id: str, tenant_id: str, *, offset: int,
        limit: int, after_sequence_no: int,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> tuple[list[AgentMessageState], int]:
        with self._session_factory() as session:
            self._require_conversation(session, conversation_id, tenant_id, owner_user_id=owner_user_id)
            filters = [AgentMessage.conversation_id == conversation_id,
                       AgentMessage.sequence_no > after_sequence_no]
            total = int(session.scalar(select(func.count()).select_from(AgentMessage).where(*filters)) or 0)
            records = session.scalars(select(AgentMessage).where(*filters)
                .order_by(AgentMessage.sequence_no, AgentMessage.id)
                .offset(offset).limit(limit)).all()
            return [self._message_state(record) for record in records], total

    def list_messages(
        self,
        conversation_id: str,
        tenant_id: str,
        *,
        after_sequence_no: int,
        limit: int | None,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> list[AgentMessageState]:
        with self._session_factory() as session:
            self._require_conversation(session, conversation_id, tenant_id, owner_user_id=owner_user_id)
            records = session.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.conversation_id == conversation_id,
                    AgentMessage.sequence_no > after_sequence_no,
                )
                .order_by(AgentMessage.sequence_no)
                .limit(limit)
            ).all()
            return [self._message_state(record) for record in records]

    def list_recent_completed_messages(
        self,
        conversation_id: str,
        tenant_id: str,
        *,
        before_sequence_no: int,
        limit: int | None,
        after_sequence_no: int = -1,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> list[AgentMessageState]:
        with self._session_factory() as session:
            self._require_conversation(session, conversation_id, tenant_id, owner_user_id=owner_user_id)
            records = session.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.conversation_id == conversation_id,
                    AgentMessage.status == AgentMessageStatus.COMPLETED.value,
                    AgentMessage.sequence_no > after_sequence_no,
                    AgentMessage.sequence_no < before_sequence_no,
                )
                .order_by(AgentMessage.sequence_no.desc())
                .limit(limit)
            ).all()
            return [self._message_state(record) for record in reversed(records)]

    def save_runtime_session(
        self,
        conversation_id: str,
        tenant_id: str,
        *,
        message_id: str,
        key: str,
        state: dict,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> None:
        with self._session_factory.begin() as session:
            record = self._require_conversation(
                session, conversation_id, tenant_id,
                for_update=True, owner_user_id=owner_user_id,
            )
            message = session.get(AgentMessage, message_id)
            if (
                message is None or message.conversation_id != conversation_id
                or message.role != AgentMessageRole.ASSISTANT.value
            ):
                raise AgentMessageNotFoundError("runtime session update requires an assistant in this conversation")
            if (
                message.status not in {AgentMessageStatus.PROCESSING.value, AgentMessageStatus.COMPLETED.value}
                or record.next_message_sequence != message.sequence_no + 1
            ):
                raise AgentConflictError("runtime session update is not for the latest active or completed assistant")
            detail = deepcopy(record.detail or {})
            sessions = detail.setdefault("runtimeSessions", {})
            sessions[key] = deepcopy(state)
            record.detail = detail

    def find_idempotent_message(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        content: str,
        requested_top_k: int,
        requested_run_options: dict[str, int],
        idempotency_key: str,
        requested_series_ids: list[str],
        reference: dict[str, str] | None = None,
        extra_context: dict | None = None,
        owner_user_id: str = LOCAL_USER_ID,
        source_tenant_id: str | None = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> StartedAgentMessageState | None:
        with self._session_factory() as session:
            self._require_conversation(session, conversation_id, tenant_id, owner_user_id=owner_user_id)
            return self._find_idempotent_message(
                session,
                conversation_id=conversation_id,
                content=content,
                requested_top_k=requested_top_k,
                requested_run_options=requested_run_options,
                idempotency_key=idempotency_key,
                requested_series_ids=requested_series_ids,
                reference=reference,
                extra_context=extra_context,
                source_tenant_id=source_tenant_id,
                attachments=attachments,
            )

    def get_message_pair(
        self, conversation_id: str, tenant_id: str, *,
        message_id: str | None = None, idempotency_key: str | None = None,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> StartedAgentMessageState:
        with self._session_factory() as session:
            self._require_conversation(session, conversation_id, tenant_id, owner_user_id=owner_user_id)
            identifier = (AgentMessage.id == message_id if message_id is not None
                          else AGENT_MESSAGE_IDEMPOTENCY_KEY == idempotency_key)
            statement = select(AgentMessage).where(
                AgentMessage.conversation_id == conversation_id,
                identifier,
            )
            if message_id is None:
                statement = statement.where(
                    AgentMessage.role == AgentMessageRole.USER.value
                )
            record = session.scalar(statement)
            if record is None:
                raise AgentMessageNotFoundError("this message has not been accepted in this conversation")
            if record.role == AgentMessageRole.USER.value:
                user = record
            elif record.role == AgentMessageRole.ASSISTANT.value:
                user = session.scalar(
                    select(AgentMessage).where(
                        AgentMessage.conversation_id == conversation_id,
                        AgentMessage.sequence_no == record.sequence_no - 1,
                        AgentMessage.role == AgentMessageRole.USER.value,
                    )
                )
            else:
                raise AgentMessageNotFoundError("this message has no user request")
            if user is None:
                raise AgentMessageNotFoundError("this message has no user request")
            assistant = session.scalar(select(AgentMessage).where(
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.sequence_no == user.sequence_no + 1,
                AgentMessage.role == AgentMessageRole.ASSISTANT.value,
            ))
            if assistant is None:
                raise AgentMessageNotFoundError("this message has no assistant reply")
            return StartedAgentMessageState(self._message_state(user), self._message_state(assistant), True)

    def checkpoint_assistant_message(
        self, message_id: str, *, now: int, phase: str | None = None,
        content: str | None = None, search_result: dict | None = None,
        timing: AgentMessageTimingState | None = None,
        render_blocks: list[dict] | None = None,
    ) -> AgentMessageState:
        with self._session_factory.begin() as session:
            record = session.get(AgentMessage, message_id, with_for_update=True)
            if record is None:
                raise AgentMessageNotFoundError("assistant message does not exist")
            if record.role != "ASSISTANT" or record.status != "PROCESSING":
                raise AgentConflictError("assistant message is not in PROCESSING state")
            changes = {name: value for name, value in {
                "phase": phase, "content": content if render_blocks is None else None,
                "search_result": search_result,
            }.items() if value is not None and getattr(record, name) != value}
            blocks_changed = (
                self._apply_render_blocks(record, render_blocks)
                if render_blocks is not None else False
            )
            if changes:
                for name, value in changes.items():
                    setattr(record, name, value)
            if changes or blocks_changed:
                record.version += 1
                record.update_time = now
            if timing is not None:
                self._apply_timing(record, timing)
                if not changes and not blocks_changed:
                    # A timing-only checkpoint did not update the parent Message before
                    # timing moved into its JSON detail column.
                    flag_modified(record, "update_time")
            if changes or blocks_changed or timing is not None:
                session.flush()
            return self._message_state(record)

    def start_message(
        self,
        *,
        conversation_id: str,
        tenant_id: str,
        content: str,
        model_name: str,
        requested_top_k: int,
        requested_run_options: dict[str, int],
        idempotency_key: str,
        series_ids: list[str],
        requested_series_ids: list[str],
        user_message_id: str,
        assistant_message_id: str,
        now: int,
        reference: dict[str, str] | None = None,
        extra_context: dict | None = None,
        owner_user_id: str = LOCAL_USER_ID,
        source_tenant_id: str | None = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> StartedAgentMessageState:
        with self._session_factory.begin() as session:
            conversation = self._require_conversation(
                session,
                conversation_id,
                tenant_id,
                for_update=True,
                owner_user_id=owner_user_id,
            )
            existing = self._find_idempotent_message(
                session,
                conversation_id=conversation_id,
                content=content,
                requested_top_k=requested_top_k,
                requested_run_options=requested_run_options,
                idempotency_key=idempotency_key,
                requested_series_ids=requested_series_ids,
                reference=reference,
                extra_context=extra_context,
                source_tenant_id=source_tenant_id,
                attachments=attachments,
            )
            if existing is not None:
                return existing

            processing = session.scalar(
                select(AgentMessage.id.label("message_id")).where(
                    AgentMessage.conversation_id == conversation_id,
                    AgentMessage.role == AgentMessageRole.ASSISTANT.value,
                    AgentMessage.status == AgentMessageStatus.PROCESSING.value,
                )
            )
            if processing is not None:
                raise AgentMessageInProgressError(
                    "this conversation already has an assistant message in progress"
                )

            user_sequence = conversation.next_message_sequence
            user = AgentMessage(
                id=user_message_id,
                conversation_id=conversation_id,
                sequence_no=user_sequence,
                role=AgentMessageRole.USER.value,
                status=AgentMessageStatus.COMPLETED.value,
                content=content,
                attachments=[dict(item) for item in attachments or ()],
                series_ids=series_ids,
                model=None,
                requested_top_k=requested_top_k,
                requested_run_options=requested_run_options,
                requested_series_ids=requested_series_ids,
                reference=reference,
                extra_context=extra_context,
                detail={"request": {"idempotencyKey": idempotency_key},
                        **({"sourceTenantId": source_tenant_id} if source_tenant_id is not None else {})},
                version=1,
            )
            assistant = AgentMessage(
                id=assistant_message_id,
                conversation_id=conversation_id,
                sequence_no=user_sequence + 1,
                role=AgentMessageRole.ASSISTANT.value,
                status=AgentMessageStatus.PROCESSING.value,
                phase=AnswerPhase.QUEUED,
                version=1,
                content=None,
                series_ids=series_ids,
                model=model_name,
                detail={
                    **({"sourceTenantId": source_tenant_id} if source_tenant_id is not None else {}),
                    "timing": {
                        "startedAt": now,
                        "totalDurationMs": None,
                        "stages": [],
                    },
                },
            )
            conversation.next_message_sequence += 2
            if user_sequence == 0 and conversation.title_is_auto:
                conversation.title = content.splitlines()[0][:40]
            conversation.last_message_time = now
            session.add(user)
            session.flush()
            session.add(assistant)
            session.flush()
            return StartedAgentMessageState(
                user_message=self._message_state(user),
                assistant_message=self._message_state(assistant),
                reused=False,
            )

    def complete_assistant_message(
        self,
        message_id: str,
        *,
        content: str,
        search_result: dict | None,
        now: int,
        timing: AgentMessageTimingState | None = None,
        render_blocks: list[dict] | None = None,
    ) -> AgentMessageState:
        with self._session_factory.begin() as session:
            record = session.get(AgentMessage, message_id, with_for_update=True)
            if record is None:
                raise AgentMessageNotFoundError(
                    f"messageId {message_id!r} does not exist"
                )
            if (
                record.role != AgentMessageRole.ASSISTANT.value
                or record.status != AgentMessageStatus.PROCESSING.value
            ):
                raise AgentConflictError(
                    "assistant message is not in PROCESSING state"
                )
            current_phase = record.phase
            record.content = content
            if render_blocks is not None:
                self._apply_render_blocks(record, render_blocks)
            self._fail_running_render_blocks(record)
            record.search_result = search_result
            record.status = AgentMessageStatus.COMPLETED.value
            record.phase = None
            record.version += 1
            record.last_error = None
            record.last_error_code = None
            record.last_error_message = None
            self._finish_timing(
                record,
                current_phase=current_phase,
                now=now,
                timing=timing,
            )
            conversation = session.get(
                AgentConversation,
                record.conversation_id,
                with_for_update=True,
            )
            assert conversation is not None
            conversation.last_message_time = now
            session.flush()
            return self._message_state(record)

    def fail_assistant_message(
        self,
        message_id: str,
        *,
        error: str,
        now: int,
        public_code: str = "PROCESSING_FAILED",
        public_message: str = "这次对话未能完成，请稍后重试。",
        timing: AgentMessageTimingState | None = None,
    ) -> AgentMessageState:
        with self._session_factory.begin() as session:
            record = session.get(AgentMessage, message_id, with_for_update=True)
            if record is None:
                raise AgentMessageNotFoundError(
                    f"messageId {message_id!r} does not exist"
                )
            if record.status == AgentMessageStatus.PROCESSING.value:
                current_phase = record.phase
                self._fail_running_render_blocks(record)
                record.status = AgentMessageStatus.FAILED.value
                record.phase = None
                record.version += 1
                record.last_error = error[:8192]
                record.last_error_code = public_code
                record.last_error_message = public_message
                self._finish_timing(
                    record,
                    current_phase=current_phase,
                    now=now,
                    timing=timing,
                )
                conversation = session.get(
                    AgentConversation,
                    record.conversation_id,
                    with_for_update=True,
                )
                assert conversation is not None
                conversation.last_message_time = now
                session.flush()
            return self._message_state(record)

    def _find_idempotent_message(
        self,
        session: Session,
        *,
        conversation_id: str,
        content: str,
        requested_top_k: int,
        requested_run_options: dict[str, int],
        idempotency_key: str,
        requested_series_ids: list[str],
        reference: dict[str, str] | None = None,
        extra_context: dict | None = None,
        source_tenant_id: str | None = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> StartedAgentMessageState | None:
        existing_user = session.scalar(
            select(AgentMessage).where(
                AgentMessage.conversation_id == conversation_id,
                AGENT_MESSAGE_IDEMPOTENCY_KEY == idempotency_key,
                AgentMessage.role == AgentMessageRole.USER.value,
            )
        )
        if existing_user is None:
            return None
        if (
            existing_user.content != content
            or existing_user.requested_top_k != requested_top_k
            or existing_user.requested_run_options != requested_run_options
            or existing_user.requested_series_ids != requested_series_ids
            or existing_user.reference != reference
            or existing_user.extra_context != extra_context
            or (existing_user.attachments or []) != (attachments or [])
            or (existing_user.detail or {}).get("sourceTenantId") != source_tenant_id
        ):
            raise AgentConflictError(
                "the message idempotency key is already used with "
                "different request fields"
            )
        assistant = session.scalar(
            select(AgentMessage).where(
                AgentMessage.conversation_id == conversation_id,
                AgentMessage.sequence_no == existing_user.sequence_no + 1,
                AgentMessage.role == AgentMessageRole.ASSISTANT.value,
            )
        )
        if assistant is None:
            raise AgentConflictError(
                "the idempotent user message has no assistant reply record"
            )
        return StartedAgentMessageState(
            user_message=self._message_state(existing_user),
            assistant_message=self._message_state(assistant),
            reused=True,
        )

    @staticmethod
    def _require_conversation(
        session: Session,
        conversation_id: str,
        tenant_id: str,
        *,
        for_update: bool = False,
        owner_user_id: str = LOCAL_USER_ID,
    ) -> AgentConversation:
        statement = select(AgentConversation).where(
            AgentConversation.id == conversation_id,
            AgentConversation.tenant_id == tenant_id,
                            AgentConversation.owner_id == owner_user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        record = session.scalar(statement)
        if record is None:
            raise AgentConversationNotFoundError(
                f"conversationId {conversation_id!r} does not exist in this workspace"
            )
        return record

    @staticmethod
    def _conversation_state(
        record: AgentConversation,
        *,
        message_count: int,
        last_message_preview: str | None = None,
    ) -> AgentConversationState:
        return AgentConversationState(
            conversation_id=record.id,
            tenant_id=record.tenant_id,
            owner_user_id=record.owner_id,
            title=record.title,
            message_count=message_count,
            last_message_at=record.last_message_time,
            created_at=record.create_time,
            updated_at=record.update_time,
            last_message_preview=last_message_preview,
            detail=record.detail,
        )

    @staticmethod
    def _message_state(record: AgentMessage) -> AgentMessageState:
        return AgentMessageState(
            message_id=record.id,
            conversation_id=record.conversation_id,
            sequence_no=record.sequence_no,
            role=record.role,
            status=record.status,
            content=record.content,
            series_ids=tuple(record.series_ids),
            model_name=record.model,
            search_result=AgentRepository._normalize_search_result_scope(record.search_result),
            last_error=(
                AgentFailure(record.last_error_code or "PROCESSING_FAILED",
                             record.last_error_message or "这次对话未能完成，请稍后重试。")
                if record.last_error is not None or record.last_error_code is not None else None
            ),
            created_at=record.create_time,
            updated_at=record.update_time,
            phase=AnswerPhase(record.phase) if record.phase is not None else None,
            version=record.version,
            reference=record.reference,
            timing=AgentRepository._timing_state(record.detail),
            extra_context=record.extra_context,
            source_tenant_id=(record.detail or {}).get("sourceTenantId"),
            attachments=tuple(dict(item) for item in record.attachments or ()),
            render_blocks=(
                tuple(deepcopy(record.detail["renderBlocks"]))
                if record.detail is not None and "renderBlocks" in record.detail else None
            ),
        )

    @staticmethod
    def _apply_render_blocks(record: AgentMessage, blocks: list[dict]) -> bool:
        blocks = deepcopy(blocks)
        content = "\n\n".join(
            block["markdown"] for block in blocks if block.get("type") == "markdown"
        )
        if (record.detail or {}).get("renderBlocks") == blocks and record.content == content:
            return False
        record.detail = {**(record.detail or {}), "renderBlocks": blocks}
        record.content = content
        return True

    @staticmethod
    def _fail_running_render_blocks(record: AgentMessage) -> None:
        blocks = (record.detail or {}).get("renderBlocks")
        if isinstance(blocks, list) and any(
            block.get("type") == "tool" and block.get("status") == "inProgress"
            for block in blocks
        ):
            AgentRepository._apply_render_blocks(record, [
                {**block, "status": "failed"}
                if block.get("type") == "tool" and block.get("status") == "inProgress"
                else block for block in blocks
            ])

    @staticmethod
    def _normalize_search_result_scope(result: dict | None) -> dict | None:
        """Read historical evidence scope keys without rewriting stored JSON."""
        if not isinstance(result, dict) or not isinstance(result.get("hits"), list):
            return result
        hits = []
        changed = False
        for hit in result["hits"]:
            evidence = hit.get("evidence") if isinstance(hit, dict) else None
            if not isinstance(evidence, dict) or not any(
                key in evidence for key in ("workspaceId", "workspace_id")
            ):
                hits.append(hit)
                continue
            values = [evidence[key] for key in (
                "tenantId", "tenant_id", "workspaceId", "workspace_id",
            ) if key in evidence]
            if any(value != values[0] for value in values[1:]):
                raise AgentReferenceInvalidError("历史证据的资源范围冲突。")
            normalized = {
                key: value for key, value in evidence.items()
                if key not in {"workspaceId", "workspace_id", "tenant_id"}
            }
            normalized["tenantId"] = values[0]
            hits.append({**hit, "evidence": normalized})
            changed = True
        return {**result, "hits": hits} if changed else result

    @staticmethod
    def _apply_timing(
        record: AgentMessage,
        timing: AgentMessageTimingState,
    ) -> None:
        if (
            timing.total_duration_ms is not None
            and (
                isinstance(timing.total_duration_ms, bool)
                or not isinstance(timing.total_duration_ms, int)
                or timing.total_duration_ms < 0
            )
        ):
            raise ValueError("total_duration_ms must be nonnegative")
        if any(
            isinstance(stage.duration_ms, bool)
            or not isinstance(stage.duration_ms, int)
            or stage.duration_ms < 0
            for stage in timing.stages
        ):
            raise ValueError("stage duration_ms must be nonnegative")
        if (
            timing.total_duration_ms is not None
            and timing.total_duration_ms
            != sum(stage.duration_ms for stage in timing.stages)
        ):
            raise ValueError("total_duration_ms must equal the stage duration sum")
        record.detail = {
            **(record.detail or {}),
            "timing": {
                "startedAt": timing.started_at,
                "totalDurationMs": timing.total_duration_ms,
                "stages": [
                    {"phase": stage.phase.value, "durationMs": stage.duration_ms}
                    for stage in timing.stages
                ],
            },
        }

    @staticmethod
    def _finish_timing(
        record: AgentMessage,
        *,
        current_phase: str | None,
        now: int,
        timing: AgentMessageTimingState | None,
    ) -> None:
        if timing is not None:
            AgentRepository._apply_timing(record, timing)
            return
        stored_timing = AgentRepository._timing_state(record.detail)
        if stored_timing is None:
            return
        stages = stored_timing.stages
        elapsed_ms = max(
            0,
            now - stored_timing.started_at,
        )
        recorded_ms = sum(stage.duration_ms for stage in stages)
        remaining_ms = max(0, elapsed_ms - recorded_ms)
        if current_phase is not None:
            stages = (
                *stages,
                AgentStageTimingState(AnswerPhase(current_phase), remaining_ms),
            )
        AgentRepository._apply_timing(
            record,
            AgentMessageTimingState(
                started_at=stored_timing.started_at,
                total_duration_ms=sum(stage.duration_ms for stage in stages),
                stages=stages,
            ),
        )

    @staticmethod
    def _timing_state(
        detail: dict | None,
    ) -> AgentMessageTimingState | None:
        if detail is None:
            return None
        if not isinstance(detail, dict):
            raise ValueError("stored Agent message detail is invalid")
        timing = detail.get("timing")
        if timing is None:
            return None
        if not isinstance(timing, dict):
            raise ValueError("stored Agent timing is invalid")
        started_at = timing.get("startedAt")
        total_duration_ms = timing.get("totalDurationMs")
        raw_stages = timing.get("stages")
        if isinstance(started_at, bool) or not isinstance(started_at, int):
            raise ValueError("stored Agent timing start is invalid")
        if not isinstance(raw_stages, list):
            raise ValueError("stored Agent stage timing is invalid")
        if (
            total_duration_ms is not None
            and (
                isinstance(total_duration_ms, bool)
                or not isinstance(total_duration_ms, int)
                or total_duration_ms < 0
            )
        ):
            raise ValueError("stored Agent total timing is invalid")
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("phase"), str)
            or isinstance(item.get("durationMs"), bool)
            or not isinstance(item.get("durationMs"), int)
            or item["durationMs"] < 0
            for item in raw_stages
        ):
            raise ValueError("stored Agent stage timing is invalid")
        stages = tuple(
            AgentStageTimingState(
                phase=AnswerPhase(item["phase"]),
                duration_ms=item["durationMs"],
            )
            for item in raw_stages
        )
        if (
            total_duration_ms is not None
            and total_duration_ms
            != sum(stage.duration_ms for stage in stages)
        ):
            raise ValueError("stored Agent total timing does not match its stages")
        return AgentMessageTimingState(
            started_at=started_at,
            total_duration_ms=total_duration_ms,
            stages=stages,
        )

    @staticmethod
    def _preview_query(conversation_id):
        return (
            select(func.substr(AgentMessage.content, 1, 120))
            .where(AgentMessage.conversation_id == conversation_id,
                   AgentMessage.content.is_not(None))
            .order_by(AgentMessage.sequence_no.desc()).limit(1)
            .correlate(AgentConversation)
        )
