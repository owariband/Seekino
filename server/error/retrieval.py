from collections.abc import Iterator
from contextlib import contextmanager
from functools import singledispatch

import httpx
from arkruntime._exceptions import ArkAPIConnectionError, ArkAPIStatusError

from engine.rag.retrieval.contracts import RetrievalUnavailableError, SeriesNotReadyError


@singledispatch
def transient_status(error: Exception) -> bool:
    """Cloud adapters register their own transport errors when loaded."""
    return False


@contextmanager
def retrieval_backend_errors() -> Iterator[None]:
    try:
        yield
    except (httpx.TransportError, ArkAPIConnectionError,
            ConnectionError, TimeoutError) as exc:
        raise RetrievalUnavailableError() from exc
    except (httpx.HTTPStatusError, ArkAPIStatusError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.status_code
        if status in {408, 429} or status >= 500:
            raise RetrievalUnavailableError() from exc
        raise
    except Exception as exc:
        if transient_status(exc):
            raise RetrievalUnavailableError() from exc
        raise


class VideoChunkNotFoundError(LookupError):
    """One or more candidate IDs have no readable current evidence."""


class SeriesScopeNotFoundError(LookupError):
    """One or more requested Series do not exist in the fact source."""
