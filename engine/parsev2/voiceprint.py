"""Persisted voice reference contract; extraction and readiness belong to the host."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


_Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
_Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _VoiceprintModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class VoiceprintModel(_VoiceprintModel):
    name: _Text
    encoder_revision: _Text


class VoiceprintReference(_VoiceprintModel):
    chunk_id: _Text
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    vector: list[float] = Field(min_length=192, max_length=192)
    source_version: int = Field(ge=1)
    event_fingerprint: _Fingerprint
    pcm_sha256: _Fingerprint
    duration_ms: int = Field(gt=0)
    selection_basis: _Text

    @model_validator(mode="after")
    def validate_reference(self) -> VoiceprintReference:
        if self.end_ms <= self.start_ms:
            raise ValueError("voice reference end_ms must exceed start_ms")
        if self.duration_ms != self.end_ms - self.start_ms:
            raise ValueError("voice reference duration_ms must match its source range")
        if not any(self.vector):
            raise ValueError("voice reference vector must be nonzero")
        return self


class CharacterVoiceprint(_VoiceprintModel):
    speaker: _Text
    model: VoiceprintModel
    references: list[VoiceprintReference] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class Voiceprint(_VoiceprintModel):
    ready: bool = False
    voiceprintList: list[CharacterVoiceprint] = Field(default_factory=list)
    # Historical inline scoring remains readable; new results use the manifest.
    scoring: dict | None = None
    method_version: _Text | None = None
    model: VoiceprintModel | None = None
    parameters: dict | None = None
    line_count: int | None = Field(default=None, ge=0)
    sample_count: int | None = Field(default=None, ge=0)
    checksum: _Fingerprint | None = None
    location: _Text | None = None

    @model_validator(mode="after")
    def validate_manifest(self):
        fields = (self.method_version, self.model, self.parameters, self.line_count,
                  self.sample_count, self.checksum, self.location)
        if any(value is not None for value in fields):
            if any(value is None for value in fields) or not self.ready:
                raise ValueError("a voiceprint artifact manifest must be complete and ready")
            if self.voiceprintList or self.scoring is not None:
                raise ValueError("voiceprint artifact manifests cannot contain inline results")
            if self.sample_count > self.line_count:
                raise ValueError("voiceprint sample_count cannot exceed line_count")
        return self

    @model_serializer(mode="wrap")
    def serialize_voiceprint(self, handler):
        values = handler(self)
        for name in ("scoring", "method_version", "model", "parameters", "line_count",
                     "sample_count", "checksum", "location"):
            if getattr(self, name) is None:
                values.pop(name, None)
        if self.location is not None:
            values.pop("voiceprintList", None)
        return values
