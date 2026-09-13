import pytest
from pydantic import ValidationError

from anivag_engine.parse.schemas import TimelineEvent


def test_timeline_event_rejects_reversed_time_range() -> None:
    with pytest.raises(ValidationError):
        TimelineEvent(
            start_ms=2000,
            end_ms=1000,
            event_type="ACTION",
            description="一名人物面对另一人双膝跪地",
            text=None,
        )
