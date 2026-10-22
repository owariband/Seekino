class VideoChunkNotFoundError(LookupError):
    """One or more candidate IDs have no readable current evidence."""


class SeriesScopeNotFoundError(LookupError):
    """One or more requested Series do not exist in the fact source."""


class SeriesNotReadyError(RuntimeError):
    """One or more requested Series have not reached the searchable state."""
