"""Model references and credentials shared by parsing and retrieval factories."""

import os
from dataclasses import dataclass
from enum import StrEnum

from gateway.config import (
    ArkModelSourceConfig,
    ModelSourceConfig,
    source_api_key_env_name,
)


class ModelReferenceType(StrEnum):
    MODEL = "model"
    ENDPOINT = "endpoint"

    @classmethod
    def for_model(cls, value: str) -> "ModelReferenceType":
        if not isinstance(value, str) or not value.strip():
            raise ValueError("model reference must be nonblank")
        return cls.ENDPOINT if value.strip().startswith("ep-") else cls.MODEL


@dataclass(frozen=True, slots=True)
class ModelReference:
    type: ModelReferenceType
    request_model: str
    model: str

    @classmethod
    def resolve(
        cls, request_model: str, *, encoder_model: str | None = None,
        default_encoder_model: str | None = None,
    ) -> "ModelReference":
        reference_type = ModelReferenceType.for_model(request_model)
        request_model = request_model.strip()
        if reference_type == ModelReferenceType.ENDPOINT:
            encoder_model = encoder_model if encoder_model is not None else default_encoder_model
            if encoder_model is None:
                raise ValueError("endpoint reference requires an explicit encoder_model")
            if ModelReferenceType.for_model(encoder_model) != ModelReferenceType.MODEL:
                raise ValueError("encoder_model must be an actual model, not an endpoint")
            model = encoder_model.strip()
        else:
            if encoder_model is not None:
                ModelReferenceType.for_model(encoder_model)
                if encoder_model.strip() != request_model:
                    raise ValueError("encoder_model differs from the direct model reference")
            model = request_model
        return cls(reference_type, request_model, model)


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
