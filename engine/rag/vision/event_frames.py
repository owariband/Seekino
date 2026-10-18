"""Event capture rules and image-vector aggregation; callers own all media I/O."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.rag._validation import _sha256_json
from engine.rag.encoding import EncoderContract, validate_embedding_vector
from engine.rag.vision.inputs import OrderedFrameSamplingContract, plan_ordered_frame_timestamps


class EventFrameContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    max_height: int = Field(gt=0, strict=True)
    fps: float = Field(default=1.0, gt=0, allow_inf_nan=False, strict=True)
    max_frames: int = Field(default=3, gt=0, strict=True)
    mime_type: Literal["image/jpeg"] = "image/jpeg"
    preprocess_revision: Literal["ffmpeg-jpeg-q3-scale-cap-v1"] = "ffmpeg-jpeg-q3-scale-cap-v1"
    strategy: Literal["uniform_midpoint_v1"] = "uniform_midpoint_v1"
    pooling: Literal["l2-mean-v1"] = "l2-mean-v1"

    def timestamps(self, start_ms: int, end_ms: int) -> tuple[int, ...]:
        return plan_ordered_frame_timestamps(start_ms, end_ms, OrderedFrameSamplingContract(
            fps=self.fps, max_frames_per_chunk=self.max_frames, strategy=self.strategy,
        ))


class CaptureFrame(BaseModel):
    """Only source positions and input digests survive temporary-file cleanup."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    requested_at_ms: int = Field(ge=0, strict=True)
    actual_at_ms: int | None = Field(default=None, ge=0, strict=True)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class EventFrameEmbeddingRequest:
    encoder: EncoderContract
    frame: CaptureFrame
    contract: EventFrameContract

    @property
    def input_sha256(self) -> str:
        return self.frame.image_sha256

    @property
    def encoder_id(self) -> str:
        return "image:sha256:" + _sha256_json({
            **self.encoder.claims(), "mime_type": self.contract.mime_type,
            "preprocess_revision": self.contract.preprocess_revision,
            "max_height": self.contract.max_height,
        })


@dataclass(frozen=True, slots=True)
class EventFrameEmbeddingArtifact:
    request: EventFrameEmbeddingRequest
    vector: tuple[float, ...]


def plan_event_frame_embeddings(
    encoder: EncoderContract,
    frames: Sequence[CaptureFrame],
    contract: EventFrameContract,
) -> tuple[EventFrameEmbeddingRequest, ...]:
    contract = EventFrameContract.model_validate(contract)
    prepared = tuple(CaptureFrame.model_validate(frame) for frame in frames)
    if not prepared or len(prepared) > contract.max_frames:
        raise ValueError("Event frames must fit the nonempty capture budget")
    if any(current.requested_at_ms <= previous.requested_at_ms
           for previous, current in zip(prepared, prepared[1:])):
        raise ValueError("Event frames must retain strictly increasing requested times")
    return tuple(EventFrameEmbeddingRequest(encoder, frame, contract) for frame in prepared)


def accept_event_frame_embedding(
    request: EventFrameEmbeddingRequest, *, modality: str, model: str,
    vector: Sequence[float],
) -> EventFrameEmbeddingArtifact:
    if modality != "image" or model != request.encoder.model:
        raise ValueError("Event image embedding response contract differs")
    return EventFrameEmbeddingArtifact(
        request, validate_embedding_vector(vector, dimensions=request.encoder.dimensions),
    )


def aggregate_event_frame_embeddings(
    requests: Sequence[EventFrameEmbeddingRequest],
    artifacts: Sequence[EventFrameEmbeddingArtifact],
    contract: EventFrameContract,
) -> tuple[float, ...]:
    """Validate every planned image and apply the explicit L2/mean/L2 contract."""

    if not requests or len(requests) != len(artifacts):
        raise ValueError("Every planned Event frame needs exactly one artifact")
    vectors = []
    for request, artifact in zip(requests, artifacts, strict=True):
        if request.contract != contract or artifact.request is not request:
            raise ValueError("Event image artifact belongs to a different plan")
        if request.encoder != requests[0].encoder:
            raise ValueError("Event images must use one encoder contract")
        vector = validate_embedding_vector(artifact.vector, dimensions=request.encoder.dimensions)
        scale = max(abs(value) for value in vector)
        scaled = tuple(value / scale for value in vector)
        norm = math.sqrt(math.fsum(value * value for value in scaled))
        vectors.append(tuple(value / norm for value in scaled))
    mean = tuple(math.fsum(values) / len(vectors) for values in zip(*vectors, strict=True))
    mean = validate_embedding_vector(mean, dimensions=requests[0].encoder.dimensions)
    norm = math.hypot(*mean)
    return tuple(value / norm for value in mean)


__all__ = [
    "EventFrameContract", "CaptureFrame", "EventFrameEmbeddingRequest",
    "EventFrameEmbeddingArtifact", "plan_event_frame_embeddings",
    "accept_event_frame_embedding", "aggregate_event_frame_embeddings",
]
