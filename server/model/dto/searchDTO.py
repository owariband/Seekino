from __future__ import annotations

from pydantic import Field

from ...schema import ContractModel


class VideoSearchRequest(ContractModel):
    workspace_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1)
    series_ids: list[str] = Field(min_length=1)
    top_k: int | None = Field(
        default=None, ge=1, strict=True,
        description="Omitted values and the upper limit follow the current topK configuration",
    )
