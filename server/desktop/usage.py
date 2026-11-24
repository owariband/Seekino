"""Project the library's existing model usage ledger for the native settings."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..model.dto.parseUsageDTO import UsageCostSummary, UsageTotals

if TYPE_CHECKING:
    from .runtime import DesktopLibrary


def overview(library: DesktopLibrary, task_id=None) -> dict:
    """Return recorded usage in the camel-case shape used by the rest of the desktop bridge."""
    state = library.usage_overview(task_id) if task_id is not None else library.usage_overview()
    return {
        "source": "local", "status": state.status, "message": state.message,
        "fromStartTime": state.from_start_time, "toStartTime": state.to_start_time,
        "fetchedAt": state.fetched_at, "truncated": state.truncated,
        "totals": _totals(state.totals),
        "models": [
            {
                **_totals(row), "provider": row.provider, "model": row.model,
                "category": row.category, "operation": row.operation, "src": row.src,
            }
            for row in state.models
        ],
        **({"segments": [{**_totals(row), "sequence": row.sequence_no} for row in state.segments]} if task_id else {}),
        "runs": [
            {
                **_totals(row), "runId": row.run_id, "taskType": row.task_type,
                "seriesId": row.series_id, "videoId": row.video_id,
            }
            for row in state.runs
        ],
    }


def _totals(totals: UsageTotals) -> dict:
    return {
        "callCount": totals.call_count, "inputTokens": totals.input_tokens,
        "outputTokens": totals.output_tokens, "cachedInputTokens": totals.cached_input_tokens,
        "cacheWriteInputTokens": totals.cache_write_input_tokens,
        "reasoningTokens": totals.reasoning_tokens, "totalTokens": totals.total_tokens,
        "knownTokens": totals.known_tokens, "missingUsageCount": totals.missing_usage_count,
        "cost": _cost(totals.cost),
    }


def _cost(cost: UsageCostSummary | None) -> dict | None:
    if cost is None:
        return None
    return {
        "currency": cost.currency, "estimatedAmount": cost.estimated_amount,
        "knownAmount": cost.known_amount, "pricedCallCount": cost.priced_call_count,
        "unpricedCallCount": cost.unpriced_call_count, "excludedCallCount": cost.excluded_call_count,
    }
