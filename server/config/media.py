from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImageUploadConfig:
    maximum_upload_size_bytes: int = 10 * 1024 * 1024
    maximum_pixels: int = 40_000_000
    model_max_edge: int = 1024

    def __post_init__(self) -> None:
        for name in (
            "maximum_upload_size_bytes", "maximum_pixels", "model_max_edge",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
