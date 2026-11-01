"""Keep the full model response until the business result gate has checked it."""
from __future__ import annotations

from typing import Any

from pydantic import RootModel

from engine.parsev2.validation import IntervalRefinementResult


class RefineModelResponse(RootModel[Any]):
    """Request the canonical schema, but retain invalid JSON values for repair.

    Parsing this transport is not acceptance. IntervalRefine must validate the
    entire value and its bound member authority before committing anything.
    """

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return IntervalRefinementResult.model_json_schema(*args, **kwargs)
