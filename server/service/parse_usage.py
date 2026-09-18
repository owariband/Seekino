from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from threading import Event

from engine.parse.usage import ParseModelCall, capture_parse_usage
from gateway.usage import TokenUsage

from ..config.usage import ModelPricingConfig, PriceTier
from ..model.dto.jobDTO import ParseJobLease
from ..model.dto.parseUsageDTO import ParseModelCallState, ParseUsageRowState, ParseUsageState
from ..repository.parse_usage import ParseUsageRepository
from ..utils.time import now_unix_time_ms
from .job import ParseJobService

_logger = logging.getLogger(__name__)


def estimate_cost_cny(usage: TokenUsage, tier: PriceTier, *, category: str, operation: str, unit: int) -> Decimal | None:
    """Count cache/audio as subsets of input, and reasoning as a subset of output."""
    if category == "embedding":
        audio = usage.audio_input_tokens
        if audio not in (None, 0):
            return None
        text, image = usage.text_input_tokens, usage.image_input_tokens
        if operation == "text_embedding" and text is None and image in (None, 0):
            text, image = usage.input_tokens, 0
        elif text == usage.input_tokens and image is None:
            image = 0
        elif image is not None and image == usage.input_tokens and text is None:
            text = 0
        if text is None or image is None or text + image != usage.input_tokens:
            return None
        parts = [(text, tier.text_input_cny_per_unit), (image, tier.image_input_cny_per_unit)]
        if usage.output_tokens:
            parts.append((usage.output_tokens, tier.output_cny_per_unit))
    else:
        audio = usage.audio_input_tokens
        if tier.audio_input_cny_per_unit is not None and audio is None:
            return None
        audio = (audio or 0) if tier.audio_input_cny_per_unit is not None else 0
        cached_input = usage.cached_input_tokens
        if cached_input is None:
            same_cache_price = tier.input_cny_per_unit == tier.cached_input_cny_per_unit
            same_audio_cache_price = tier.audio_input_cny_per_unit == tier.cached_audio_input_cny_per_unit
            if usage.input_tokens and (not same_cache_price or (audio and not same_audio_cache_price)):
                return None
            cached_input = 0
        cached_audio = usage.cached_audio_input_tokens
        if audio == 0 or cached_input == 0:
            cached_audio = 0
        if cached_audio is None:
            return None
        cached_text = cached_input - cached_audio
        plain = usage.input_tokens - audio - cached_text
        if min(cached_audio, cached_text, plain, audio - cached_audio) < 0:
            return None
        parts = [(plain, tier.input_cny_per_unit), (cached_text, tier.cached_input_cny_per_unit),
                 (audio - cached_audio, tier.audio_input_cny_per_unit),
                 (cached_audio, tier.cached_audio_input_cny_per_unit), (usage.output_tokens, tier.output_cny_per_unit)]
    if any(count and rate is None for count, rate in parts):
        return None
    return sum((Decimal(count) * rate for count, rate in parts if count and rate is not None), Decimal(0)) / Decimal(unit)


class ParseUsageService:
    def __init__(self, repository: ParseUsageRepository, jobs: ParseJobService, pricing: ModelPricingConfig) -> None:
        self._repository = repository
        self._jobs = jobs
        self._pricing = pricing

    @contextmanager
    def capture(self, lease: ParseJobLease) -> Iterator[None]:
        incomplete = Event()
        try:
            if not self._repository.start_attempt(lease, now=now_unix_time_ms()):
                incomplete.set()
        except Exception:
            incomplete.set()
            _logger.warning("Could not start Parse usage recording job_id=%s", lease.job_id)

        def observe(event: ParseModelCall) -> None:
            try:
                self.record(lease, event)
            except Exception:
                incomplete.set()
                _logger.warning("Could not persist Parse model usage job_id=%s", lease.job_id)

        try:
            with capture_parse_usage(observe):
                yield
        finally:
            try:
                self._repository.finish_attempt(lease, recording_complete=not incomplete.is_set())
            except Exception:
                _logger.warning("Could not finalize Parse usage recording job_id=%s", lease.job_id)

    def record(self, lease: ParseJobLease, event: ParseModelCall) -> ParseModelCallState:
        usage = event.call.usage
        snapshot = self._pricing.snapshot(provider=event.call.provider, model=event.call.model,
                                          category=event.category, input_tokens=usage.input_tokens,
                                          started_at=event.call.started_at) if usage is not None else None
        cost = estimate_cost_cny(usage, PriceTier.model_validate(snapshot["tier"]), category=event.category,
                                 operation=event.call.operation, unit=snapshot["unit"]) if usage is not None and snapshot is not None else None
        return self._repository.record_call(lease, event, estimated_cost_cny=cost, price_snapshot=snapshot)

    def get(self, job_id: str, *, workspace_id: str) -> ParseUsageState:
        job = self._jobs.get(job_id, workspace_id=workspace_id).job
        ledger = self._repository.snapshot(job.job_id, workspace_id=job.workspace_id)
        calls, attempts = ledger.calls, ledger.attempts
        if not attempts and not calls:
            coverage = "not_started" if ledger.attempt_count == 0 and ledger.job_status == "QUEUED" else "unknown"
        elif any(not complete for complete in attempts.values()) or set(attempts) != set(range(1, ledger.attempt_count + 1)):
            coverage = "partial"
        else:
            coverage = "recorded"
        groups: dict[tuple[str, str, str, str], list[ParseModelCallState]] = {}
        for call in calls:
            groups.setdefault((call.provider, call.model, call.category, call.operation), []).append(call)
        items = tuple(self._row(identity, group) for identity, group in sorted(groups.items()))
        missing = sum(item.missing_usage_count for item in items)
        unpriced = sum(item.unpriced_call_count for item in items)
        known_tokens = [item.total_tokens for item in items if item.total_tokens is not None]
        known_costs = [item.estimated_cost_cny for item in items if item.estimated_cost_cny is not None]
        observed_empty = coverage == "recorded" and not calls
        return ParseUsageState(
            job=job, coverage=coverage,
            total_tokens=sum(known_tokens) if known_tokens else 0 if observed_empty else None,
            estimated_cost_cny=sum(known_costs, Decimal(0)) if known_costs else Decimal(0) if observed_empty else None,
            call_count=len(calls), missing_usage_count=missing, unpriced_call_count=unpriced, items=items,
        )

    @staticmethod
    def _row(identity: tuple[str, str, str, str], calls: list[ParseModelCallState]) -> ParseUsageRowState:
        missing = sum(call.usage is None for call in calls)
        unpriced = sum(call.usage is not None and call.estimated_cost_cny is None for call in calls)
        def tokens(field: str) -> int | None:
            values = [getattr(call.usage, field) for call in calls if call.usage is not None]
            return None if not values or any(value is None for value in values) else sum(values)
        known_costs = [call.estimated_cost_cny for call in calls if call.estimated_cost_cny is not None]
        cost = sum(known_costs, Decimal(0)) if known_costs else None
        provider, model, category, operation = identity
        return ParseUsageRowState(
            provider=provider, model=model, category=category, operation=operation, call_count=len(calls),
            input_tokens=tokens("input_tokens"), output_tokens=tokens("output_tokens"),
            cached_input_tokens=tokens("cached_input_tokens"), reasoning_tokens=tokens("reasoning_tokens"),
            total_tokens=tokens("total_tokens"), estimated_cost_cny=cost,
            missing_usage_count=missing, unpriced_call_count=unpriced,
        )
