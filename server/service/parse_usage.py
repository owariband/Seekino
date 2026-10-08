from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from gateway.tracing import LangfuseTracing
from gateway.usage import model_call_context

from ..model.dto.jobDTO import (
    ChunkParseLease, IntervalParseLease, ParseJobLease, PrefixParseLease,
    VideoParseLease, VideoParseTaskInputState,
)
from ..model.dto.parseUsageDTO import (
    UsageModelRow, UsageOverviewState, UsageRunRow, UsageTotals,
)
from ..storage.langfuse import LangfuseObservationStore, LangfuseQueryError
from ..utils.time import datetime_to_unix_time_ms, now_utc, unix_time_ms_to_datetime
from ..utils.validation import require_nonempty_text

_logger = logging.getLogger(__name__)
_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "reasoning_tokens", "total_tokens",
)


class ParseUsageService:
    """Read model consumption from Langfuse; capture only binds model context."""

    def __init__(
        self, store: LangfuseObservationStore, *, tracing: LangfuseTracing | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._store, self._tracing, self._clock = store, tracing, clock

    def close(self) -> None:
        self._store.close()

    @contextmanager
    def capture(self, lease: ParseJobLease) -> Iterator[None]:
        context = {
            "workspace_id": lease.workspace_id, "series_id": lease.series_id,
            "video_id": lease.video_id, "job_id": lease.job_id,
            "run_id": lease.job_id, "session_id": lease.video_id,
            "attempt_count": lease.attempt_count, "lease_version": lease.lease_version,
            "category": "parse",
        }
        tracing = self._tracing.scope(context) if self._tracing is not None else nullcontext()
        with model_call_context(context), tracing:
            yield

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
            "lease_version": lease.lease_version, "sequence_no": sequence_no,
            "category": category,
        }
        tracing = self._tracing.scope(context) if self._tracing is not None else nullcontext()
        with model_call_context(context), tracing:
            yield

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

        status = self._store.status
        records: list[dict[str, Any]] = []
        truncated = False
        message = None
        if status == "available":
            try:
                batch = self._store.query(
                    workspace_id=workspace_id, from_start_time=start, to_start_time=end,
                )
                truncated = batch.truncated
                seen: set[str] = set()
                for observation in batch.observations:
                    if str(observation.get("type") or "").upper() not in {"GENERATION", "EMBEDDING"}:
                        continue
                    identity = _text(observation.get("id"))
                    if identity is None:
                        truncated = True
                        continue
                    if identity in seen:
                        continue
                    seen.add(identity)
                    metadata = _mapping(observation.get("metadata"))
                    if metadata.get("workspace_id") != workspace_id:
                        continue
                    series_ids = metadata.get("series_ids")
                    if isinstance(series_ids, str):
                        series_ids = _json_value(series_ids)
                    if series_id is not None and metadata.get("series_id") != series_id and (
                        not isinstance(series_ids, (list, tuple)) or series_id not in series_ids
                    ):
                        continue
                    record = _model_usage(observation, metadata)
                    if run_id is not None and record["run_id"] != run_id:
                        continue
                    records.append(record)
            except LangfuseQueryError:
                status, message = "unavailable", "Langfuse 暂时无法读取模型消耗，请稍后重试。"
                _logger.warning("Langfuse consumption query failed")
        elif status == "disabled":
            message = "Langfuse 模型消耗记录未启用。"
        else:
            message = "Langfuse 尚未配置完整的地址和凭据。"

        if status != "available":
            return UsageOverviewState(
                status=status, message=message, from_start_time=from_start_time,
                to_start_time=to_start_time, fetched_at=fetched_at, truncated=False,
                totals=_unknown_totals(), models=(), runs=(),
            )
        models: dict[tuple, list[dict]] = {}
        runs: dict[str, list[dict]] = {}
        for record in records:
            key = tuple(record[name] for name in ("provider", "model", "category", "operation"))
            models.setdefault(key, []).append(record)
            runs.setdefault(record["run_id"], []).append(record)
        model_rows = tuple(
            UsageModelRow(**asdict(_totals(group, truncated=truncated)),
                          provider=key[0], model=key[1], category=key[2], operation=key[3])
            for key, group in sorted(models.items(), key=lambda pair: tuple(value or "" for value in pair[0]))
        )
        run_rows = tuple(
            UsageRunRow(**asdict(_totals(group, truncated=truncated)), run_id=identity,
                        kind=_unique(group, "kind") or "other", series_id=_unique(group, "series_id"),
                        video_id=_unique(group, "video_id"))
            for identity, group in sorted(runs.items())
        )
        return UsageOverviewState(
            status="available", message=None, from_start_time=from_start_time,
            to_start_time=to_start_time, fetched_at=fetched_at, truncated=truncated,
            totals=_totals(records, truncated=truncated), models=model_rows, runs=run_rows,
        )


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        value = _json_value(value)
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _counter(*values: Any) -> int | None:
    for value in values:
        if type(value) is int and value >= 0:
            return value
        if type(value) is float and math.isfinite(value) and value >= 0 and value.is_integer():
            return int(value)
    return None


def _amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _cost(observation: Mapping[str, Any], metadata: Mapping[str, Any]) -> Decimal | None:
    details = _mapping(observation.get("costDetails"))
    if metadata.get("cost_source") == "sdk_estimate":
        return _amount(details.get("total"))
    if metadata.get("usage_pricing_status") == "incomplete" or metadata.get("usage_details_semantics") != "exclusive_buckets":
        return None
    usage_details = _mapping(observation.get("usageDetails"))
    for name, value in usage_details.items():
        if name == "total":
            continue
        count = _counter(value)
        if count is None or (count > 0 and _amount(details.get(name)) is None):
            return None
    for value in (observation.get("providedTotalCost"), observation.get("totalCost"), details.get("total")):
        amount = _amount(value)
        if amount is None:
            continue
        # Langfuse may project an unpriced model as zero; require a pricing basis.
        if amount == 0 and not (
            observation.get("modelId") or observation.get("internalModelId")
            or observation.get("providedTotalCost") is not None
        ):
            continue
        return amount
    if details and "total" not in details:
        amounts = [_amount(value) for value in details.values()]
        if all(amount is not None for amount in amounts):
            total = sum(amounts, Decimal(0))
            if total > 0 or observation.get("modelId") or observation.get("internalModelId"):
                return total
    return None


def _model_usage(observation: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any]:
    details = _mapping(observation.get("usageDetails"))
    subsets = _mapping(metadata.get("usage_subsets"))
    input_buckets = [value for name, value in details.items() if name == "input" or name.startswith("input_")]
    exclusive_input = (
        sum(input_buckets) if input_buckets and all(type(value) is int and value >= 0 for value in input_buckets)
        else None
    )
    input_tokens = _counter(
        observation.get("inputUsage"), subsets.get("input_tokens"),
        exclusive_input if metadata.get("usage_details_semantics") == "exclusive_buckets" else details.get("input"),
        details.get("input_tokens"),
    )
    output_tokens = _counter(observation.get("outputUsage"), subsets.get("output_tokens"), details.get("output"), details.get("output_tokens"))
    total_tokens = _counter(observation.get("totalUsage"), details.get("total"), details.get("total_tokens"))
    if metadata.get("usage_status") == "unknown" or (
        input_tokens == output_tokens == total_tokens == 0 and not details
        and metadata.get("usage_status") != "known"
    ):
        input_tokens = output_tokens = total_tokens = None
    if input_tokens is not None and output_tokens is not None:
        if total_tokens is None:
            total_tokens = input_tokens + output_tokens
        elif total_tokens != input_tokens + output_tokens:
            input_tokens = output_tokens = total_tokens = None
    cached = _counter(
        subsets.get("cached_input_tokens"), details.get("input_cached"), details.get("input_cached_tokens"),
        details.get("cache_read_input_tokens"), details.get("cached_input_tokens"),
    )
    cache_write = _counter(
        subsets.get("cache_write_input_tokens"), details.get("input_cache_creation_tokens"), details.get("input_cache_write"),
        details.get("cache_write_input_tokens"), details.get("cache_creation_input_tokens"),
    )
    creation_buckets = [details[name] for name in (
        "input_cache_creation_5m_tokens", "input_cache_creation_1h_tokens",
    ) if name in details]
    if cache_write is None and creation_buckets and all(type(value) is int and value >= 0 for value in creation_buckets):
        cache_write = sum(creation_buckets)
    reasoning = _counter(
        subsets.get("reasoning_tokens"), details.get("output_reasoning"),
        details.get("output_reasoning_tokens"), details.get("reasoning_tokens"),
    )
    if input_tokens is not None:
        if cached is not None and cached > input_tokens:
            cached = None
        if cache_write is not None and cache_write > input_tokens:
            cache_write = None
    if output_tokens is not None and reasoning is not None and reasoning > output_tokens:
        reasoning = None
    kind: Literal["parse", "chat", "other"] = (
        "chat" if metadata.get("assistant_message_id") or metadata.get("conversation_id")
        else "parse" if metadata.get("video_parse_task_id") or metadata.get("job_id") else "other"
    )
    run_id = next((value for value in (
        _text(metadata.get("run_id")), _text(metadata.get("video_parse_task_id")),
        _text(metadata.get("job_id")), _text(metadata.get("assistant_message_id")),
        _text(observation.get("traceId")), _text(observation.get("id")),
    ) if value is not None))
    series_id = _text(metadata.get("series_id"))
    series_ids = metadata.get("series_ids")
    if isinstance(series_ids, str):
        series_ids = _json_value(series_ids)
    if series_id is None and isinstance(series_ids, (list, tuple)) and len(series_ids) == 1:
        series_id = _text(series_ids[0])
    usage_complete = input_tokens is not None and output_tokens is not None and total_tokens is not None
    cost = _cost(observation, metadata) if usage_complete or metadata.get("cost_source") == "sdk_estimate" else None
    return {
        "call_id": _text(metadata.get("call_id")) or observation["id"],
        "provider": _text(metadata.get("provider")),
        "model": _text(observation.get("model")) or _text(observation.get("modelName")) or _text(observation.get("providedModelName")),
        "category": _text(metadata.get("category")), "operation": _text(metadata.get("operation")),
        "run_id": run_id, "kind": kind, "series_id": series_id,
        "video_id": _text(metadata.get("video_id")),
        "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens,
        "cached_input_tokens": cached, "cache_write_input_tokens": cache_write,
        "reasoning_tokens": reasoning, "cost": cost,
        "known_tokens": total_tokens if total_tokens is not None else (input_tokens or 0) + (output_tokens or 0),
        "usage_complete": usage_complete,
    }


def _unique(records: list[dict], name: str) -> Any:
    values = {record[name] for record in records if record[name] is not None}
    return next(iter(values)) if len(values) == 1 else None


def _totals(records: list[dict], *, truncated: bool) -> UsageTotals:
    token_totals = {
        name: None if truncated or any(record[name] is None for record in records)
        else sum(record[name] for record in records)
        for name in _TOKEN_FIELDS
    }
    if any(not record["usage_complete"] for record in records):
        token_totals["total_tokens"] = None
    cost = sum((record["cost"] for record in records if record["cost"] is not None), Decimal(0))
    return UsageTotals(
        call_count=len({record["call_id"] for record in records}), **token_totals,
        known_tokens=sum(record["known_tokens"] for record in records),
        estimated_cost_usd=None if truncated or any(record["cost"] is None for record in records) else cost,
        known_cost_usd=cost,
        missing_usage_count=len({record["call_id"] for record in records if not record["usage_complete"]}),
        unpriced_call_count=len({record["call_id"] for record in records if record["cost"] is None}),
    )


def _unknown_totals() -> UsageTotals:
    return UsageTotals(
        call_count=None, input_tokens=None, output_tokens=None,
        cached_input_tokens=None, cache_write_input_tokens=None, reasoning_tokens=None,
        total_tokens=None, known_tokens=0, estimated_cost_usd=None, known_cost_usd=Decimal(0),
        missing_usage_count=0, unpriced_call_count=0,
    )
