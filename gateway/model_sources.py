"""Credential resolution shared by parsing and retrieval model factories."""

import os

from gateway.config import (
    ArkModelSourceConfig,
    ModelSourceConfig,
    source_api_key_env_name,
)


def source_key(source: ModelSourceConfig) -> str:
    name = source_api_key_env_name(source)
    key = os.getenv(name or "", "").strip()
    if not key:
        raise RuntimeError(
            f"model API key environment variable {name!r} is not configured"
        )
    return key


def ark_options(source: ArkModelSourceConfig) -> dict[str, str]:
    # Legacy calls resolve ARK_API_KEY lazily inside the SDK, as before.
    return (
        {} if source.api_key_env == "ARK_API_KEY" else {"api_key": source_key(source)}
    )
