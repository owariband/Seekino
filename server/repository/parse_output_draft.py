"""Short-transaction persistence for one call's latest Parse output draft."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..model.dto.parseOutputDraftDTO import ParseOutputDraftState
from ..model.parse_output_draft import ParseModelOutputDraft
from ..utils.time import require_utc_datetime
from ..utils.validation import require_nonempty_text


class ParseOutputDraftConflictError(ValueError):
    pass


def _validate_identity(state: ParseOutputDraftState) -> None:
    require_nonempty_text(state.call_id, "call_id", maximum=36)
    try:
        canonical_call_id = str(UUID(state.call_id))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("call_id must be a canonical UUID") from error
    if canonical_call_id != state.call_id:
        raise ValueError("call_id must be a canonical UUID")
    require_nonempty_text(state.tenant_id, "tenant_id", maximum=64)
    require_nonempty_text(state.video_task_id, "video_task_id", maximum=36)
    if state.actor_user_id is not None:
        require_nonempty_text(state.actor_user_id, "actor_user_id", maximum=64)
    if state.started_at is not None:
        require_utc_datetime(state.started_at, "started_at")


def _check_owner(record: ParseModelOutputDraft, state: ParseOutputDraftState) -> None:
    if (record.tenant_id, record.video_task_id, record.user_id) != (
        state.tenant_id, state.video_task_id, state.actor_user_id,
    ):
        raise ParseOutputDraftConflictError(
            "call_id already belongs to a different tenant/task/actor"
        )


class ParseOutputDraftRepository:
    """Store cumulative text only for the draft table; never lock the Parse task."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def save(self, state: ParseOutputDraftState) -> ParseOutputDraftState:
        _validate_identity(state)
        with self._session_factory.begin() as session:
            record = session.get(ParseModelOutputDraft, state.call_id)
            if record is None:
                record = ParseModelOutputDraft(
                    id=state.call_id, tenant_id=state.tenant_id,
                    video_task_id=state.video_task_id, user_id=state.actor_user_id,
                    text=state.text, status=state.status,
                    metadata_=dict(state.metadata), start_time=state.started_at,
                )
                session.add(record)
                session.flush()
                return self._state(record)
            _check_owner(record, state)
            if record.status != "streaming":
                if record.status == state.status and record.text == state.text:
                    return self._state(record)
                raise ParseOutputDraftConflictError("draft is already terminal")
            if state.text_bytes < len(record.text.encode("utf-8")):
                raise ParseOutputDraftConflictError(
                    "an older draft snapshot cannot replace newer text"
                )
            if not state.text.startswith(record.text):
                raise ParseOutputDraftConflictError("draft text must extend the saved prefix")
            record.text = state.text
            record.status = state.status
            record.metadata_ = dict(state.metadata)
            if record.start_time is None:
                record.start_time = state.started_at
            session.flush()
            return self._state(record)

    def get(
        self, *, tenant_id: str, video_task_id: str, call_id: str,
    ) -> ParseOutputDraftState | None:
        require_nonempty_text(tenant_id, "tenant_id", maximum=64)
        require_nonempty_text(video_task_id, "video_task_id", maximum=36)
        require_nonempty_text(call_id, "call_id", maximum=36)
        with self._session_factory() as session:
            record = session.scalar(select(ParseModelOutputDraft).where(
                ParseModelOutputDraft.id == call_id,
                ParseModelOutputDraft.tenant_id == tenant_id,
                ParseModelOutputDraft.video_task_id == video_task_id,
            ))
            if (
                record is None
                or record.tenant_id != tenant_id
                or record.video_task_id != video_task_id
            ):
                return None
            return self._state(record)

    @staticmethod
    def _state(record: ParseModelOutputDraft) -> ParseOutputDraftState:
        return ParseOutputDraftState(
            call_id=record.id, tenant_id=record.tenant_id,
            video_task_id=record.video_task_id, actor_user_id=record.user_id,
            text=record.text, text_bytes=len(record.text.encode("utf-8")),
            status=record.status, metadata=record.metadata_,
            started_at=record.start_time,
        )
