from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from arkruntime._exceptions import ArkAPIConnectionError, ArkAPIStatusError
from elastic_transport import ConnectionError as ElasticConnectionError
from elastic_transport import ConnectionTimeout
from elasticsearch import ApiError

from engine.rag.retrieval.contracts import RetrievalUnavailableError, SeriesNotReadyError


@contextmanager
def retrieval_backend_errors() -> Iterator[None]:
    try:
        yield
    except (httpx.TransportError, ArkAPIConnectionError, ElasticConnectionError, ConnectionTimeout,
            ConnectionError, TimeoutError) as exc:
        raise RetrievalUnavailableError() from exc
    except (httpx.HTTPStatusError, ArkAPIStatusError, ApiError) as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.status_code
        if status in {408, 429} or status >= 500:
            raise RetrievalUnavailableError() from exc
        raise


class VideoChunkNotFoundError(LookupError):
    """One or more candidate IDs have no readable current evidence."""


class SeriesScopeNotFoundError(LookupError):
    """One or more requested Series do not exist in the fact source."""
