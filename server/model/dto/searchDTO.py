from __future__ import annotations

from pydantic import Field

from ...schema import ContractModel


class VideoSearchRequest(ContractModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1)
    series_ids: list[str] = Field(min_length=1)
    top_k: int = Field(default=10, gt=0, le=100)
