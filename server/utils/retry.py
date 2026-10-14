from datetime import timedelta
from math import ldexp, log2


def task_retry_delay(
    initial_delay_seconds: float,
    max_delay_seconds: float,
    failure_count: int,
) -> timedelta:
    """Apply the task snapshot's backoff policy after a failed attempt."""
    exponent = max(0, failure_count - 1)
    if exponent >= log2(max_delay_seconds) - log2(initial_delay_seconds):
        return timedelta(seconds=max_delay_seconds)
    return timedelta(seconds=ldexp(initial_delay_seconds, exponent))
