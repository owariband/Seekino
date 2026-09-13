from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class EmbeddingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    modality: Literal["text", "video"]
    vector: list[float] = Field(min_length=1)

    @computed_field
    @property
    def dimension(self) -> int:
        return len(self.vector)
