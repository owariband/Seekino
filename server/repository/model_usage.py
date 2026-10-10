from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from gateway.usage import TokenUsage

from ..model.dto.parseUsageDTO import ModelCallUsageBatch, ModelUsageState
from ..model.parse_usage import ModelUsage
from ..utils.time import datetime_to_unix_time_ms, require_utc_datetime
from ..utils.validation import require_nonempty_text

MAX_USAGE_RECORDS = 20_000


class ModelUsageConflictError(ValueError):
    pass


def _json_values(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_values(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_values(item) for item in value]
    return value


def _validate_tokens(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) != set(TokenUsage.__dataclass_fields__):
        raise ValueError("tokens must be null or the fixed TokenUsage object")
    TokenUsage(**value)


def _check_owner(record: ModelUsage, state: ModelUsageState) -> None:
    if (record.tenant_id, record.task_type, record.run_id, record.user_id) != (
        state.tenant_id, state.task_type, state.run_id, state.actor_user_id,
    ):
        raise ModelUsageConflictError("call_id already belongs to a different tenant/task/run/actor")


def _duplicate_call_id(error: IntegrityError) -> bool:
    original = error.orig
    if getattr(original, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY:
        return "model_usage.id" in str(original)
    return bool(
        original.args and original.args[0] == 1062
        and "PRIMARY" in str(original.args[1:])
    )


class ModelUsageRepository:
    """Append each call once; every read is scoped before sorting and limiting."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record_call(self, state: ModelUsageState) -> ModelUsageState:
        require_nonempty_text(state.call_id, "call_id", maximum=36)
        if str(UUID(state.call_id)) != state.call_id:
            raise ValueError("call_id must be a canonical UUID")
        require_nonempty_text(state.tenant_id, "tenant_id", maximum=64)
        require_nonempty_text(state.run_id, "run_id", maximum=256)
        if state.actor_user_id is not None:
            require_nonempty_text(state.actor_user_id, "actor_user_id", maximum=64)
        if state.task_type not in {"video_parse", "chat_retrieval"}:
            raise ValueError("task_type must be video_parse or chat_retrieval")
        require_utc_datetime(state.started_at, "started_at")
        require_utc_datetime(state.created_at, "created_at")
        if set(state.usage) != {"tokens", "sdk_model_usage"}:
            raise ValueError("usage must contain tokens and sdk_model_usage")
        _validate_tokens(state.usage["tokens"])
        details = state.usage["sdk_model_usage"]
        if not isinstance(details, tuple):
            raise ValueError("sdk_model_usage must be a JSON array")
        for detail in details:
            if not isinstance(detail, Mapping) or set(detail) != {"model_name", "tokens"}:
                raise ValueError("SDK usage must contain model_name and tokens")
            if not isinstance(detail["model_name"], str) or not detail["model_name"].strip():
                raise ValueError("SDK model_name must be nonempty")
            _validate_tokens(detail["tokens"])
        if set(state.metadata) != {
            "model_name", "provider", "src", "operation", "status", "request_id",
            "ended_at", "granularity", "usage_source", "usage_scope", "context",
        } or not isinstance(state.metadata["context"], Mapping):
            raise ValueError("metadata must be the fixed model-call object with a context object")
        try:
            with self._session_factory.begin() as session:
                existing = session.get(ModelUsage, state.call_id)
                if existing is not None:
                    _check_owner(existing, state)
                    return self._state(existing)
                record = ModelUsage(
                    id=state.call_id, tenant_id=state.tenant_id,
                    user_id=state.actor_user_id,
                    task_type=state.task_type, run_id=state.run_id,
                    start_time=state.started_at, create_time=datetime_to_unix_time_ms(state.created_at),
                    usage=_json_values(state.usage), metadata_=_json_values(state.metadata),
                )
                session.add(record)
                session.flush()
                # Read back millisecond-normalized UTC values from the database.
                session.refresh(record)
                return self._state(record)
        except IntegrityError as error:
            if not _duplicate_call_id(error):
                raise
            with self._session_factory() as session:
                existing = session.get(ModelUsage, state.call_id)
                if existing is None:
                    raise
                _check_owner(existing, state)
                return self._state(existing)

    def query(
        self, *, tenant_id: str, from_start_time: datetime,
        to_start_time: datetime, series_id: str | None = None,
        run_id: str | None = None,
        actor_user_id: str | None = None,
    ) -> ModelCallUsageBatch:
        require_nonempty_text(tenant_id, "tenant_id", maximum=64)
        start = require_utc_datetime(from_start_time, "from_start_time")
        end = require_utc_datetime(to_start_time, "to_start_time")
        if start >= end:
            raise ValueError("usage window must be positive")
        filters = [
            ModelUsage.tenant_id == tenant_id,
            ModelUsage.start_time >= start,
            ModelUsage.start_time < end,
        ]
        if actor_user_id is not None:
            require_nonempty_text(actor_user_id, "actor_user_id", maximum=64)
            filters.append(ModelUsage.user_id == actor_user_id)
        if run_id is not None:
            require_nonempty_text(run_id, "run_id", maximum=256)
            filters.append(ModelUsage.run_id == run_id)
        if series_id is not None:
            require_nonempty_text(series_id, "series_id", maximum=64)
            context = ModelUsage.metadata_["context"]
            if self._session_factory.kw["bind"].dialect.name == "sqlite":
                values = func.json_each(context["series_ids"]).table_valued("value")
                matching = and_(
                    func.json_type(context["series_ids"]) == "array",
                    select(values.c.value).where(values.c.value == series_id).exists(),
                )
                scalar = and_(
                    func.json_type(context["series_id"]) == "text",
                    context["series_id"].as_string() == series_id,
                )
            else:
                # JSON string comparison stays case-sensitive regardless of connection collation.
                scalar = and_(
                    func.json_type(context["series_id"]) == "STRING",
                    func.json_contains(context["series_id"], json.dumps(series_id)) == 1,
                )
                matching = and_(
                    func.json_type(context["series_ids"]) == "ARRAY",
                    func.json_contains(context["series_ids"], json.dumps(series_id)) == 1,
                )
            filters.append(or_(scalar, matching))
        statement = select(ModelUsage).where(*filters).order_by(
            ModelUsage.start_time, ModelUsage.id,
        ).limit(MAX_USAGE_RECORDS + 1)
        with self._session_factory() as session:
            records = session.scalars(statement).all()
            return ModelCallUsageBatch(
                records=tuple(self._state(record) for record in records[:MAX_USAGE_RECORDS]),
                truncated=len(records) > MAX_USAGE_RECORDS,
            )

    @staticmethod
    def _state(record: ModelUsage) -> ModelUsageState:
        return ModelUsageState(
            call_id=record.id, tenant_id=record.tenant_id,
            actor_user_id=record.user_id,
            task_type=record.task_type, run_id=record.run_id,
            started_at=record.start_time, usage=record.usage,
            metadata=record.metadata_, created_at=record.create_date,
        )
