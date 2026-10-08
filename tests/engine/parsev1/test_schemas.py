import pytest
from pydantic import ValidationError

from engine.parsev1.schemas import TimelineEvent, TranscriptCue


def test_transcript_cue_accepts_point_time_range() -> None:
    cue = TranscriptCue(start_seconds=3000, end_seconds=3000, text="短句")

    assert cue.start_seconds == cue.end_seconds == 3000


def test_timeline_event_rejects_reversed_time_range() -> None:
    with pytest.raises(ValidationError):
        TimelineEvent(
            start_seconds=2000,
            end_seconds=1000,
            event_type="ACTION",
            description="一名人物面对另一人双膝跪地",
            text=None,
        )
