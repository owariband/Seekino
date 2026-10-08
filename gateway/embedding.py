from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class EmbeddingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    modality: Literal["text", "image", "video"]
    vector: list[float] = Field(min_length=1)

    @computed_field
    @property
    def dimension(self) -> int:
        return len(self.vector)


class SparseEmbeddingResult(BaseModel):
    """Provider-neutral sparse indices and nonnegative activation weights."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, pattern=r"\S")
    weights: dict[str, Annotated[float, Field(ge=0, allow_inf_nan=False, strict=True)]]
