"""Errors specific to video search draft validation."""


class InvalidDraftRangeError(ValueError):
    """A model-submitted Draft range is outside its canonical Evidence."""

    def __init__(
        self,
        *,
        video_chunk_id: str,
        start_ms: int,
        end_ms: int,
        evidence_start_ms: int,
        evidence_end_ms: int,
    ) -> None:
        self.video_chunk_id = video_chunk_id
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.evidence_start_ms = evidence_start_ms
        self.evidence_end_ms = evidence_end_ms
        super().__init__(
            "Draft Item range must satisfy canonical Evidence bounds and "
            "start_ms < end_ms"
        )
