from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..database import DatabaseEnvironment


_MAX_INDEX_GENERATION = (1 << 31) - 1


@dataclass(frozen=True, slots=True)
class ElasticsearchConfig:
    url: str
    index_name: str
    api_key: str | None = field(default=None, repr=False)
    index_generation: int = 1

    @classmethod
    def from_environment(
        cls, environment: DatabaseEnvironment,
    ) -> "ElasticsearchConfig | None":
        url = os.getenv("ANIVAG_ELASTICSEARCH_URL", "").strip()
        index_name = os.getenv("ANIVAG_ELASTICSEARCH_INDEX", "").strip()
        api_key = os.getenv("ANIVAG_ELASTICSEARCH_API_KEY") or None
        raw_index_generation = os.getenv(
            "ANIVAG_ELASTICSEARCH_INDEX_GENERATION", ""
        ).strip()
        if not (url or index_name or api_key or raw_index_generation):
            return None
        if not url or not index_name:
            raise ValueError(
                "ANIVAG_ELASTICSEARCH_URL and ANIVAG_ELASTICSEARCH_INDEX "
                "must be configured together"
            )
        try:
            index_generation = int(raw_index_generation or "1")
        except ValueError:
            raise ValueError(
                "ANIVAG_ELASTICSEARCH_INDEX_GENERATION must be a positive integer"
            ) from None
        config = cls(
            url=url,
            index_name=index_name,
            api_key=api_key,
            index_generation=index_generation,
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
            or not 1 <= self.index_generation <= _MAX_INDEX_GENERATION
        ):
            raise ValueError(
                "Elasticsearch index_generation must be a positive integer "
                "within the external version contract"
            )

    def validate_environment(self, environment: DatabaseEnvironment | str) -> None:
        selected = DatabaseEnvironment(environment)
        if self.index_name != self.index_name.lower():
            raise ValueError("Elasticsearch index name must be lowercase")
        parts = set(self.index_name.replace("-", "_").split("_"))
        if selected is DatabaseEnvironment.TEST and "test" not in parts:
            raise ValueError("test Elasticsearch index name must contain a test component")
        if selected is DatabaseEnvironment.DEVELOPMENT and not parts.intersection(
            {"dev", "development"}
        ):
            raise ValueError("development Elasticsearch index name must contain a dev component")
        if selected is DatabaseEnvironment.PRODUCTION and parts.intersection(
            {"test", "dev", "development"}
        ):
            raise ValueError("production Elasticsearch index name cannot name test or dev")
