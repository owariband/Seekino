from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..model.agent import (
    AgentConversationRecord,
    AgentMessageRecord,
    AgentMessageRole,
    AgentMessageStatus,
    AgentSessionRecord,
)


class AgentSessionNotFoundError(LookupError):
    pass


class AgentConversationNotFoundError(LookupError):
    pass


class AgentMessageNotFoundError(LookupError):
    pass


class AgentConflictError(RuntimeError):
    pass


class AgentMessageInProgressError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgentSessionState:
    session_id: str
    workspace_id: str
    name: str
    conversation_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentConversationState:
    conversation_id: str
    session_id: str
    workspace_id: str
    title: str
    series_ids: tuple[str, ...]
    message_count: int
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AgentMessageState:
    message_id: str
    conversation_id: str
    sequence_no: int
    role: str
    status: str
    content: str | None
    model_name: str | None
    reply_to_message_id: str | None
    retrieval_query: str | None
    search_result: dict | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StartedAgentMessageState:
    user_message: AgentMessageState
    assistant_message: AgentMessageState
    reused: bool


class AgentRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_session(
        self,
        *,
        session_id: str,
        workspace_id: str,
        name: str,
        idempotency_key: str,
    ) -> AgentSessionState:
        for attempt in range(2):
            try:
                with self._session_factory.begin() as session:
                    existing = session.scalar(
                        select(AgentSessionRecord).where(
                            AgentSessionRecord.workspace_id == workspace_id,
                            AgentSessionRecord.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        if existing.name != name:
                            raise AgentConflictError(
                                "the session idempotency key is already used with "
                                "another name"
                            )
                        conversation_count = session.scalar(
                            select(
                                func.count(AgentConversationRecord.conversation_id)
                            ).where(
                                AgentConversationRecord.session_id
                                == existing.session_id
                            )
                        )
                        return self._session_state(
                            existing,
                            conversation_count=int(conversation_count or 0),
                        )

                    record = AgentSessionRecord(
                        session_id=session_id,
                        workspace_id=workspace_id,
                        name=name,
                        idempotency_key=idempotency_key,
                    )
                    session.add(record)
                    session.flush()
                    return self._session_state(record, conversation_count=0)
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    def list_sessions(
        self,
        workspace_id: str,
        *,
        limit: int,
    ) -> list[AgentSessionState]:
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    AgentSessionRecord,
                    func.count(AgentConversationRecord.conversation_id),
                )
                .outerjoin(
                    AgentConversationRecord,
                    AgentConversationRecord.session_id == AgentSessionRecord.session_id,
                )
                .where(AgentSessionRecord.workspace_id == workspace_id)
                .group_by(AgentSessionRecord.session_id)
                .order_by(
                    AgentSessionRecord.updated_at.desc(),
                    AgentSessionRecord.session_id.desc(),
                )
                .limit(limit)
            ).all()
            return [
                self._session_state(record, conversation_count=int(count))
                for record, count in rows
            ]

    def get_session(self, session_id: str, workspace_id: str) -> AgentSessionState:
        with self._session_factory() as session:
            record = self._require_session(session, session_id, workspace_id)
            conversation_count = session.scalar(
                select(func.count(AgentConversationRecord.conversation_id)).where(
                    AgentConversationRecord.session_id == session_id
                )
            )
            return self._session_state(
                record,
                conversation_count=int(conversation_count or 0),
            )

    def create_conversation(
        self,
        *,
        conversation_id: str,
        session_id: str,
        workspace_id: str,
        title: str,
        series_ids: list[str],
        idempotency_key: str,
    ) -> AgentConversationState:
        for attempt in range(2):
            try:
                with self._session_factory.begin() as session:
                    self._require_session(session, session_id, workspace_id)
                    existing = session.scalar(
                        select(AgentConversationRecord).where(
                            AgentConversationRecord.session_id == session_id,
                            AgentConversationRecord.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is not None:
                        if existing.title != title or existing.series_ids != series_ids:
                            raise AgentConflictError(
                                "the conversation idempotency key is already used "
                                "with different fields"
                            )
                        message_count = session.scalar(
                            select(func.count(AgentMessageRecord.message_id)).where(
                                AgentMessageRecord.conversation_id
                                == existing.conversation_id
                            )
                        )
                        return self._conversation_state(
                            existing,
                            workspace_id=workspace_id,
                            message_count=int(message_count or 0),
                        )

                    record = AgentConversationRecord(
                        conversation_id=conversation_id,
                        session_id=session_id,
                        title=title,
                        series_ids=series_ids,
                        idempotency_key=idempotency_key,
                        next_message_sequence=0,
                    )
                    session.add(record)
                    session.flush()
                    return self._conversation_state(
                        record,
                        workspace_id=workspace_id,
                        message_count=0,
                    )
            except IntegrityError:
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    def list_conversations(
        self,
        session_id: str,
        workspace_id: str,
        *,
        limit: int,
    ) -> list[AgentConversationState]:
        with self._session_factory() as session:
            self._require_session(session, session_id, workspace_id)
            rows = session.execute(
                select(
                    AgentConversationRecord,
                    func.count(AgentMessageRecord.message_id),
                )
                .outerjoin(
                    AgentMessageRecord,
                    AgentMessageRecord.conversation_id
                    == AgentConversationRecord.conversation_id,
                )
                .where(AgentConversationRecord.session_id == session_id)
                .group_by(AgentConversationRecord.conversation_id)
                .order_by(
                    AgentConversationRecord.last_message_at.desc(),
                    AgentConversationRecord.created_at.desc(),
                    AgentConversationRecord.conversation_id.desc(),
                )
                .limit(limit)
            ).all()
            return [
                self._conversation_state(
                    record,
                    workspace_id=workspace_id,
                    message_count=int(count),
                )
                for record, count in rows
            ]

    def get_conversation(
        self,
        conversation_id: str,
        workspace_id: str,
    ) -> AgentConversationState:
        with self._session_factory() as session:
            record, actual_workspace = self._require_conversation(
                session,
                conversation_id,
                workspace_id,
            )
            message_count = session.scalar(
                select(func.count(AgentMessageRecord.message_id)).where(
                    AgentMessageRecord.conversation_id == conversation_id
                )
            )
            return self._conversation_state(
                record,
                workspace_id=actual_workspace,
                message_count=int(message_count or 0),
            )

    def list_messages(
        self,
        conversation_id: str,
        workspace_id: str,
        *,
        after_sequence_no: int,
        limit: int | None,
    ) -> list[AgentMessageState]:
        with self._session_factory() as session:
            self._require_conversation(session, conversation_id, workspace_id)
            records = session.scalars(
                select(AgentMessageRecord)
                .where(
                    AgentMessageRecord.conversation_id == conversation_id,
                    AgentMessageRecord.sequence_no > after_sequence_no,
                )
                .order_by(AgentMessageRecord.sequence_no)
                .limit(limit)
            ).all()
            return [self._message_state(record) for record in records]

    def list_recent_completed_messages(
        self,
        conversation_id: str,
        workspace_id: str,
        *,
        before_sequence_no: int,
        limit: int,
    ) -> list[AgentMessageState]:
        with self._session_factory() as session:
            self._require_conversation(session, conversation_id, workspace_id)
            records = session.scalars(
                select(AgentMessageRecord)
                .where(
                    AgentMessageRecord.conversation_id == conversation_id,
                    AgentMessageRecord.status == AgentMessageStatus.COMPLETED.value,
                    AgentMessageRecord.sequence_no < before_sequence_no,
                )
                .order_by(AgentMessageRecord.sequence_no.desc())
                .limit(limit)
            ).all()
            return [self._message_state(record) for record in reversed(records)]

    def start_message(
        self,
        *,
        conversation_id: str,
        workspace_id: str,
        content: str,
        model_name: str,
        requested_top_k: int,
        idempotency_key: str,
        user_message_id: str,
        assistant_message_id: str,
        now: datetime,
    ) -> StartedAgentMessageState:
        with self._session_factory.begin() as session:
            conversation, _workspace = self._require_conversation(
                session,
                conversation_id,
                workspace_id,
                for_update=True,
            )
            existing_user = session.scalar(
                select(AgentMessageRecord).where(
                    AgentMessageRecord.conversation_id == conversation_id,
                    AgentMessageRecord.idempotency_key == idempotency_key,
                )
            )
            if existing_user is not None:
                if (
                    existing_user.role != AgentMessageRole.USER.value
                    or existing_user.content != content
                    or existing_user.requested_top_k != requested_top_k
                ):
                    raise AgentConflictError(
                        "the message idempotency key is already used with "
                        "different request fields"
                    )
                assistant = session.scalar(
                    select(AgentMessageRecord).where(
                        AgentMessageRecord.reply_to_message_id
                        == existing_user.message_id
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

            processing = session.scalar(
                select(AgentMessageRecord.message_id).where(
                    AgentMessageRecord.conversation_id == conversation_id,
                    AgentMessageRecord.role == AgentMessageRole.ASSISTANT.value,
                    AgentMessageRecord.status == AgentMessageStatus.PROCESSING.value,
                )
            )
            if processing is not None:
                raise AgentMessageInProgressError(
                    "this conversation already has an assistant message in progress"
                )

            user_sequence = conversation.next_message_sequence
            user = AgentMessageRecord(
                message_id=user_message_id,
                conversation_id=conversation_id,
                sequence_no=user_sequence,
                role=AgentMessageRole.USER.value,
                status=AgentMessageStatus.COMPLETED.value,
                content=content,
                model_name=None,
                idempotency_key=idempotency_key,
                requested_top_k=requested_top_k,
            )
            assistant = AgentMessageRecord(
                message_id=assistant_message_id,
                conversation_id=conversation_id,
                sequence_no=user_sequence + 1,
                role=AgentMessageRole.ASSISTANT.value,
                status=AgentMessageStatus.PROCESSING.value,
                content=None,
                model_name=model_name,
                reply_to_message_id=user_message_id,
            )
            conversation.next_message_sequence += 2
            conversation.last_message_at = now
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
        retrieval_query: str,
        search_result: dict,
        now: datetime,
    ) -> AgentMessageState:
        with self._session_factory.begin() as session:
            record = session.get(AgentMessageRecord, message_id, with_for_update=True)
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
            record.content = content
            record.retrieval_query = retrieval_query
            record.search_result = search_result
            record.status = AgentMessageStatus.COMPLETED.value
            record.last_error = None
            conversation = session.get(
                AgentConversationRecord,
                record.conversation_id,
                with_for_update=True,
            )
            assert conversation is not None
            conversation.last_message_at = now
            session.flush()
            return self._message_state(record)

    def fail_assistant_message(
        self,
        message_id: str,
        *,
        error: str,
        now: datetime,
    ) -> AgentMessageState:
        with self._session_factory.begin() as session:
            record = session.get(AgentMessageRecord, message_id, with_for_update=True)
            if record is None:
                raise AgentMessageNotFoundError(
                    f"messageId {message_id!r} does not exist"
                )
            if record.status == AgentMessageStatus.PROCESSING.value:
                record.status = AgentMessageStatus.FAILED.value
                record.content = "这次检索没有完成，请稍后重试。"
                record.last_error = error[:8192]
                conversation = session.get(
                    AgentConversationRecord,
                    record.conversation_id,
                    with_for_update=True,
                )
                assert conversation is not None
                conversation.last_message_at = now
                session.flush()
            return self._message_state(record)

    @staticmethod
    def _require_session(
        session: Session,
        session_id: str,
        workspace_id: str,
    ) -> AgentSessionRecord:
        record = session.scalar(
            select(AgentSessionRecord).where(
                AgentSessionRecord.session_id == session_id,
                AgentSessionRecord.workspace_id == workspace_id,
            )
        )
        if record is None:
            raise AgentSessionNotFoundError(
                f"sessionId {session_id!r} does not exist in this workspace"
            )
        return record

    @staticmethod
    def _require_conversation(
        session: Session,
        conversation_id: str,
        workspace_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[AgentConversationRecord, str]:
        statement = (
            select(AgentConversationRecord, AgentSessionRecord.workspace_id)
            .join(
                AgentSessionRecord,
                AgentSessionRecord.session_id == AgentConversationRecord.session_id,
            )
            .where(
                AgentConversationRecord.conversation_id == conversation_id,
                AgentSessionRecord.workspace_id == workspace_id,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        row = session.execute(statement).one_or_none()
        if row is None:
            raise AgentConversationNotFoundError(
                f"conversationId {conversation_id!r} does not exist in this workspace"
            )
        return row[0], row[1]

    @staticmethod
    def _session_state(
        record: AgentSessionRecord,
        *,
        conversation_count: int,
    ) -> AgentSessionState:
        return AgentSessionState(
            session_id=record.session_id,
            workspace_id=record.workspace_id,
            name=record.name,
            conversation_count=conversation_count,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _conversation_state(
        record: AgentConversationRecord,
        *,
        workspace_id: str,
        message_count: int,
    ) -> AgentConversationState:
        return AgentConversationState(
            conversation_id=record.conversation_id,
            session_id=record.session_id,
            workspace_id=workspace_id,
            title=record.title,
            series_ids=tuple(record.series_ids),
            message_count=message_count,
            last_message_at=record.last_message_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _message_state(record: AgentMessageRecord) -> AgentMessageState:
        return AgentMessageState(
            message_id=record.message_id,
            conversation_id=record.conversation_id,
            sequence_no=record.sequence_no,
            role=record.role,
            status=record.status,
            content=record.content,
            model_name=record.model_name,
            reply_to_message_id=record.reply_to_message_id,
            retrieval_query=record.retrieval_query,
            search_result=record.search_result,
            last_error=record.last_error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
