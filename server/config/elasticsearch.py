from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..database import DatabaseEnvironment
from ..constants.parse import MAX_INDEX_GENERATION


DEFAULT_INDEX_GENERATION = 1
_DEFAULT_VECTOR_DIMENSIONS = 1024


def resolve_vector_dimensions(
    *, text_dimensions: int | str | None = None,
    vision_dimensions: int | str | None = None,
    require_matching: bool = False,
) -> tuple[int, int]:
    text = int(text_dimensions if text_dimensions is not None else os.getenv(
        "ANIVAG_TEXT_VECTOR_DIMENSIONS", str(_DEFAULT_VECTOR_DIMENSIONS),
    ))
    vision = int(vision_dimensions if vision_dimensions is not None else os.getenv(
        "ANIVAG_VISION_VECTOR_DIMENSIONS", str(_DEFAULT_VECTOR_DIMENSIONS),
    ))
    if text <= 0 or vision <= 0:
        raise ValueError("text and vision vector dimensions must be greater than zero")
    if require_matching and text != vision:
        raise ValueError("text and vision vector dimensions must match for shared query vectors")
    return text, vision


@dataclass(frozen=True, slots=True)
class ElasticsearchConfig:
    url: str
    index_name: str
    api_key: str | None = field(default=None, repr=False)
    index_generation: int = DEFAULT_INDEX_GENERATION
    event_index_name: str | None = None

    @classmethod
    def from_environment(
        cls, environment: DatabaseEnvironment | str, *,
        url: str | None = None,
        index_name: str | None = None,
        api_key: str | None = None,
        index_generation: int | str | None = None,
        event_index_name: str | None = None,
    ) -> "ElasticsearchConfig | None":
        url = (url if url is not None else os.getenv("ANIVAG_ELASTICSEARCH_URL", "")).strip()
        index_name = (index_name if index_name is not None else os.getenv(
            "ANIVAG_ELASTICSEARCH_INDEX", "",
        )).strip()
        api_key = (api_key if api_key is not None else os.getenv("ANIVAG_ELASTICSEARCH_API_KEY")) or None
        raw_index_generation = str(index_generation if index_generation is not None else os.getenv(
            "ANIVAG_ELASTICSEARCH_INDEX_GENERATION", "",
        )).strip()
        if not (url or index_name or api_key or raw_index_generation):
            return None
        if not url or not index_name:
            raise ValueError(
                "ANIVAG_ELASTICSEARCH_URL and ANIVAG_ELASTICSEARCH_INDEX "
                "must be configured together"
            )
        try:
            index_generation = int(raw_index_generation or str(DEFAULT_INDEX_GENERATION))
        except ValueError:
            raise ValueError(
                "ANIVAG_ELASTICSEARCH_INDEX_GENERATION must be a positive integer"
            ) from None
        config = cls(
            url=url,
            index_name=index_name,
            api_key=api_key,
            index_generation=index_generation,
            event_index_name=event_index_name if event_index_name is not None else os.getenv(
                "SEEKINO_ELASTICSEARCH_EVENT_INDEX", "",
            ).strip() or None,
        )
        config.validate_environment(environment)
        return config

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or "@" in parsed.netloc
            or parsed.path.rstrip("/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Elasticsearch URL must be an HTTP(S) origin without credentials")
        if not self.index_name or self.index_name != self.index_name.strip():
            raise ValueError("Elasticsearch index name must not be empty or padded")
        if (
            isinstance(self.index_generation, bool)
            or not isinstance(self.index_generation, int)
            or not 1 <= self.index_generation <= MAX_INDEX_GENERATION
        ):
            raise ValueError(
                "Elasticsearch index_generation must be a positive integer "
                "within the external version contract"
            )

    def validate_environment(self, environment: DatabaseEnvironment | str) -> None:
        validate_index_name(self.index_name, environment)
        event_index = self.event_index_for_environment(environment)
        validate_index_name(event_index, environment)
        if event_index == self.index_name:
            raise ValueError("Event and Chunk indices must use different physical names")

    def event_index_for_environment(self, environment: DatabaseEnvironment | str) -> str:
        return self.event_index_name or f"seekino_video_events_{DatabaseEnvironment(environment).value}_v1"


def validate_index_name(index_name: str, environment: DatabaseEnvironment | str) -> None:
    selected = DatabaseEnvironment(environment)
    if not isinstance(index_name, str) or not index_name or index_name != index_name.strip():
        raise ValueError("Elasticsearch index name must not be empty or padded")
    if index_name != index_name.lower():
        raise ValueError("Elasticsearch index name must be lowercase")
    parts = set(index_name.replace("-", "_").split("_"))
    if selected is DatabaseEnvironment.TEST and "test" not in parts:
        raise ValueError("test Elasticsearch index name must contain a test component")
    if selected is DatabaseEnvironment.DEVELOPMENT and not parts.intersection({"dev", "development"}):
        raise ValueError("development Elasticsearch index name must contain a dev component")
    if selected is DatabaseEnvironment.PRODUCTION and parts.intersection({"test", "dev", "development"}):
        raise ValueError("production Elasticsearch index name cannot name test or dev")
