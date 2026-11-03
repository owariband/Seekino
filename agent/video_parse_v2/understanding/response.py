"""Retain the complete candidate until Understanding's business gate accepts it."""
from __future__ import annotations

from typing import Any

from pydantic import RootModel

from engine.parsev2.schemas import VideoParseResult


class UnderstandingModelResponse(RootModel[Any]):
    """Advertise the canonical schema without discarding an invalid candidate."""

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return VideoParseResult.model_json_schema(*args, **kwargs)
