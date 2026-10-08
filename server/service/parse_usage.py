from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Literal

from gateway.tracing import LangfuseTracing
from gateway.usage import ModelCallUsage, TokenUsage, capture_model_usage, model_call_context
from sqlalchemy.exc import SQLAlchemyError

from ..model.dto.jobDTO import (
    ChunkParseLease, IntervalParseLease, ParseJobLease, PrefixParseLease,
    VideoParseLease, VideoParseTaskInputState,
)
from ..model.dto.parseUsageDTO import (
    ModelUsageState,
    UsageModelRow, UsageOverviewState, UsageRunRow, UsageTotals,
)
from ..repository.model_usage import ModelUsageRepository
from ..utils.time import datetime_to_unix_time_ms, now_utc, unix_time_ms_to_datetime
from ..utils.validation import require_nonempty_text

_logger = logging.getLogger(__name__)
_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "reasoning_tokens", "total_tokens",
)
_CONTEXT_FIELDS = {
    "series_id", "series_ids", "video_id", "video_ids", "session_id",
    "conversation_id", "user_message_id", "video_parse_task_ids",
    "chunk_parse_task_id", "interval_parse_task_id", "video_chunk_id",
    "execution_kind", "execution_task_id", "category", "stage", "sequence_no",
    "lease_version", "attempt_count", "retry_count", "index_generation",
}


class ParseUsageService:
    """Capture owned Gateway calls in short SQL transactions and report recorded usage."""

    def __init__(
        self, repository: ModelUsageRepository, *, tracing: LangfuseTracing | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._repository, self._tracing, self._clock = repository, tracing, clock
        # Gateway de-duplicates subscribers by callable identity across nested scopes.
        self._observer = self._record_call
        self._owner: ContextVar[tuple[str, str, str] | None] = ContextVar("local_usage_owner", default=None)

    def close(self) -> None:
        pass  # The application owns the shared engine and its lifetime.

    @contextmanager
    def capture(self, lease: ParseJobLease) -> Iterator[None]:
        context = {
            "workspace_id": lease.workspace_id, "series_id": lease.series_id,
            "video_id": lease.video_id, "job_id": lease.job_id,
            "run_id": lease.job_id, "session_id": lease.video_id,
            "attempt_count": lease.attempt_count, "lease_version": lease.lease_version,
            "category": "parse",
            "task_type": None,
        }
        tracing = self._tracing.scope(context) if self._tracing is not None else nullcontext()
        owner_token = self._owner.set(None)
        try:
            with model_call_context(context), tracing:
                yield
        finally:
            self._owner.reset(owner_token)

    @contextmanager
    def capture_video(
        self, lease: VideoParseLease | ChunkParseLease | IntervalParseLease | PrefixParseLease,
        task_input: VideoParseTaskInputState, *, sequence_no: int | None,
        category: Literal["parse", "embedding"],
    ) -> Iterator[None]:
        if category not in {"parse", "embedding"}:
            raise ValueError("category must be parse or embedding")
        if sequence_no is not None and (type(sequence_no) is not int or sequence_no < 0):
            raise ValueError("sequence_no must be a nonnegative integer")
        context = {
            "workspace_id": task_input.workspace_id, "series_id": task_input.series_id,
            "video_id": task_input.task.video_id, "video_parse_task_id": lease.video_parse_task_id,
            "run_id": lease.video_parse_task_id, "session_id": task_input.task.video_id,
            "task_type": "video_parse",
            "lease_version": lease.lease_version, "sequence_no": sequence_no,
            "category": category,
        }
        tracing = self._tracing.scope(context) if self._tracing is not None else nullcontext()
        owner_token = self._owner.set((task_input.workspace_id, "video_parse", lease.video_parse_task_id))
        try:
            with model_call_context(context), tracing, capture_model_usage(self._observer, combine=True):
                yield
        finally:
            self._owner.reset(owner_token)

    @contextmanager
    def capture_chat(self, context: Mapping[str, Any]) -> Iterator[None]:
        workspace_id = require_nonempty_text(context.get("workspace_id"), "workspace_id", maximum=64)
        message_id = require_nonempty_text(context.get("assistant_message_id"), "assistant_message_id", maximum=256)
        owner_token = self._owner.set((workspace_id, "chat_retrieval", message_id))
        try:
            with model_call_context({
                **context, "workspace_id": workspace_id, "task_type": "chat_retrieval", "run_id": message_id,
            }), capture_model_usage(self._observer, combine=True):
                yield
        finally:
            self._owner.reset(owner_token)

    def _record_call(self, call: ModelCallUsage) -> None:
        context = call.context
        task_type = context.get("task_type")
        if task_type not in {"video_parse", "chat_retrieval"} or self._owner.get() is None:
            return
        try:
            workspace_id = require_nonempty_text(context.get("workspace_id"), "workspace_id", maximum=64)
            run_id = require_nonempty_text(context.get("run_id"), "run_id", maximum=256)
            identity = "video_parse_task_id" if task_type == "video_parse" else "assistant_message_id"
            other_identity = "assistant_message_id" if task_type == "video_parse" else "video_parse_task_id"
            if (
                (workspace_id, task_type, run_id) != self._owner.get()
                or context.get(identity) != run_id or context.get(other_identity) is not None
            ):
                raise ValueError("model call context conflicts with its owner")
            self._repository.record_call(ModelUsageState(
                call_id=call.call_id, workspace_id=workspace_id, task_type=task_type, run_id=run_id,
                started_at=unix_time_ms_to_datetime(call.started_at), created_at=self._clock(),
                usage={
                    "tokens": asdict(call.usage) if call.usage is not None else None,
                    "sdk_model_usage": [{
                        "model_name": detail.model,
                        "tokens": asdict(detail.usage) if detail.usage is not None else None,
                    } for detail in call.sdk_model_usage],
                },
                metadata={
                    "model_name": call.model, "provider": call.provider, "src": context.get("src"),
                    "operation": call.operation, "status": call.status, "request_id": call.request_id,
                    "ended_at": call.ended_at, "granularity": call.granularity,
                    "usage_source": call.usage_source, "usage_scope": call.usage_scope,
                    "context": {key: value for key, value in context.items() if key in _CONTEXT_FIELDS},
                },
            ))
        except Exception as error:
            _logger.warning(
                "Model usage append failed; model result is preserved call_id=%s task_type=%s error=%s",
                call.call_id, task_type, type(error).__name__,
            )

    def overview(
        self, *, workspace_id: str, from_start_time: int | None = None,
        to_start_time: int | None = None, series_id: str | None = None,
        run_id: str | None = None,
    ) -> UsageOverviewState:
        workspace_id = require_nonempty_text(workspace_id, "workspace_id", maximum=64)
        if series_id is not None:
            series_id = require_nonempty_text(series_id, "series_id", maximum=64)
        if run_id is not None:
            run_id = require_nonempty_text(run_id, "run_id", maximum=256)
        fetched_at = datetime_to_unix_time_ms(self._clock())
        to_start_time = (fetched_at // 60_000 + 1) * 60_000 if to_start_time is None else to_start_time
        from_start_time = to_start_time - 30 * 86_400_000 if from_start_time is None else from_start_time
        start, end = unix_time_ms_to_datetime(from_start_time), unix_time_ms_to_datetime(to_start_time)
        if start >= end or end - start > timedelta(days=31):
            raise ValueError("usage window must be positive and no longer than 31 days")

        try:
            batch = self._repository.query(
                workspace_id=workspace_id, from_start_time=start, to_start_time=end,
                series_id=series_id, run_id=run_id,
            )
        except SQLAlchemyError as error:
            _logger.warning("Model usage query failed workspace_id=%s error=%s", workspace_id, type(error).__name__)
            return UsageOverviewState(
                status="unavailable", message="模型消耗暂时无法读取，请稍后重试。",
                from_start_time=from_start_time, to_start_time=to_start_time,
                fetched_at=fetched_at, truncated=False,
                totals=_unknown_totals(), models=(), runs=(),
            )
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for state in batch.records:
            if state.call_id in seen:
                continue
            seen.add(state.call_id)
            details = state.usage["sdk_model_usage"]
            if details:
                records.extend(_model_usage(state, detail["model_name"], detail["tokens"]) for detail in details)
            else:
                records.append(_model_usage(state, state.metadata["model_name"], state.usage["tokens"]))
        models: dict[tuple, list[dict]] = {}
        runs: dict[tuple[str, str], list[dict]] = {}
        for record in records:
            key = tuple(record[name] for name in ("provider", "model", "category", "operation"))
            models.setdefault(key, []).append(record)
            runs.setdefault((record["task_type"], record["run_id"]), []).append(record)
        model_rows = tuple(
            UsageModelRow(**asdict(_totals(group, truncated=batch.truncated)),
                          provider=key[0], model=key[1], category=key[2], operation=key[3])
            for key, group in sorted(models.items(), key=lambda pair: tuple(value or "" for value in pair[0]))
        )
        run_rows = tuple(
            UsageRunRow(**asdict(_totals(group, truncated=batch.truncated)), run_id=identity,
                        task_type=task_type, series_id=_unique(group, "series_id"), video_id=_unique(group, "video_id"))
            for (task_type, identity), group in sorted(runs.items())
        )
        return UsageOverviewState(
            status="available", message=None, from_start_time=from_start_time,
            to_start_time=to_start_time, fetched_at=fetched_at, truncated=batch.truncated,
            totals=_totals(records, truncated=batch.truncated), models=model_rows, runs=run_rows,
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _model_usage(state: ModelUsageState, model: Any, tokens: Mapping | None) -> dict[str, Any]:
    # Persisted tokens follow Gateway's inclusive contract; subsets never enter total twice.
    usage = None
    if tokens is not None:
        try:
            usage = TokenUsage(**tokens)
        except (TypeError, ValueError):
            pass
    metadata, context = state.metadata, state.metadata["context"]
    series_id = _text(context.get("series_id"))
    series_ids = context.get("series_ids")
    if series_id is None and isinstance(series_ids, tuple) and len(series_ids) == 1:
        series_id = _text(series_ids[0])
    return {
        "call_id": state.call_id, "provider": _text(metadata["provider"]), "model": _text(model),
        "category": _text(context.get("category")), "operation": _text(metadata["operation"]),
        "run_id": state.run_id, "task_type": state.task_type,
        "series_id": series_id, "video_id": _text(context.get("video_id")),
        **{name: getattr(usage, name) if usage is not None else None for name in _TOKEN_FIELDS},
        "known_tokens": usage.total_tokens if usage is not None else 0,
        "usage_complete": usage is not None and metadata["usage_scope"] != "main_loop",
    }


def _unique(records: list[dict], name: str) -> Any:
    values = {record[name] for record in records}
    return next(iter(values)) if len(values) == 1 else None


def _totals(records: list[dict], *, truncated: bool) -> UsageTotals:
    token_totals = {
        name: None if truncated or any(record[name] is None for record in records)
        else sum(record[name] for record in records)
        for name in _TOKEN_FIELDS
    }
    if any(not record["usage_complete"] for record in records):
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            token_totals[name] = None
    return UsageTotals(
        call_count=len({record["call_id"] for record in records}), **token_totals,
        known_tokens=sum(record["known_tokens"] for record in records),
        missing_usage_count=len({record["call_id"] for record in records if not record["usage_complete"]}),
    )


def _unknown_totals() -> UsageTotals:
    return UsageTotals(
        call_count=None, input_tokens=None, output_tokens=None,
        cached_input_tokens=None, cache_write_input_tokens=None, reasoning_tokens=None,
        total_tokens=None, known_tokens=0, missing_usage_count=0,
    )
