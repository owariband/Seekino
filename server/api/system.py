from __future__ import annotations


from ..utils.time import utc_now
from ..model.vo.systemVO import HealthView


def health() -> HealthView:
    return HealthView(server_time=utc_now())
