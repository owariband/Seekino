from __future__ import annotations

from dataclasses import replace

import pytest

from server.service.embedding_reuse import (
    EmbeddingReuseDecision,
    EmbeddingReuseTarget,
    assess_embedding_reuse,
)

INPUT_SHA = "a" * 64


def _target(modality="text"):
    return EmbeddingReuseTarget(
        modality=modality,
        input_sha256=INPUT_SHA,
        provider="ark",
        model="endpoint-v1",
        encoder_revision="gateway-ark-embedding-v1",
        dimensions=2,
        normalization="none",
        output_contract="float-vector-v1",
        video_fps=1.0 if modality == "video" else None,
        mime_type="video/mp4" if modality == "video" else None,
    )


def _metadata(target):
    result = {
        "input_sha256": target.input_sha256,
        "modality": target.modality,
        "provider": target.provider,
        "model": target.model,
        "encoder_revision": target.encoder_revision,
        "dimensions": target.dimensions,
        "normalization": target.normalization,
        "output_contract": target.output_contract,
    }
    if target.modality == "video":
        result.update(video_fps=target.video_fps, mime_type=target.mime_type)
    return result


@pytest.mark.parametrize("modality", ["text", "video"])
def test_native_v1_artifact_reuses_only_complete_matching_contract(modality) -> None:
    target = _target(modality)

    decision = assess_embedding_reuse(
        target,
        source_kind="v1_native_artifact",
        metadata=_metadata(target),
        vector=[0.6, 0.8],
    )

    assert decision == EmbeddingReuseDecision((0.6, 0.8), None)


def test_import_parse_video_stat_hash_cannot_prove_media_bytes() -> None:
    target = _target("video")

    decision = assess_embedding_reuse(
        target,
        source_kind="import_parse_cache",
        metadata=_metadata(target),
        vector=[0.6, 0.8],
    )

    assert decision.gap == "UNPROVEN_INPUT"
    assert decision.vector is None


def test_import_parse_text_can_use_verified_text_hash_with_complete_encoder() -> None:
    target = _target()
    decision = assess_embedding_reuse(
        target,
        source_kind="import_parse_cache",
        metadata=_metadata(target),
        vector=[1.0, 0.0],
    )
    assert decision.vector == (1.0, 0.0)


@pytest.mark.parametrize("modality", ["text", "video"])
def test_v2_index_cache_reuses_the_same_proven_contract(modality) -> None:
    target = _target(modality)
    decision = assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=_metadata(target),
        vector=[0.6, 0.8],
    )
    assert decision.vector == (0.6, 0.8)


@pytest.mark.parametrize(
    "missing", [
        "provider", "model", "encoder_revision", "dimensions",
        "normalization", "output_contract", "video_fps", "mime_type",
    ],
)
def test_missing_video_encoder_claim_reports_gap(missing) -> None:
    target = _target("video")
    metadata = _metadata(target)
    metadata.pop(missing)

    decision = assess_embedding_reuse(
        target,
        source_kind="v1_native_artifact",
        metadata=metadata,
        vector=[0.6, 0.8],
    )

    assert decision.gap == "UNPROVEN_ENCODER"
    assert decision.vector is None


def test_existing_v1_mysql_metadata_is_not_assumed_compatible() -> None:
    target = _target("video")
    old_metadata = {
        "input_sha256": INPUT_SHA,
        "modality": "video",
        "model": "endpoint-v1",
        "dimensions": 2,
        "video_chunk_id": "old-chunk",
    }

    decision = assess_embedding_reuse(
        target,
        source_kind="v1_native_artifact",
        metadata=old_metadata,
        vector=[0.6, 0.8],
    )

    assert decision.gap == "UNPROVEN_ENCODER"


@pytest.mark.parametrize(
    ("field", "value"), [
        ("modality", "text"),
        ("provider", "other-provider"),
        ("model", "other-model"),
        ("encoder_revision", "other-revision"),
        ("dimensions", 3),
        ("normalization", "unit"),
        ("output_contract", "int8-vector-v1"),
        ("video_fps", 2.0),
        ("mime_type", "video/quicktime"),
    ],
)
def test_changed_encoder_or_video_input_contract_rejects_reuse(field, value) -> None:
    target = _target("video")
    metadata = _metadata(target)
    metadata[field] = value

    decision = assess_embedding_reuse(
        target, source_kind="v1_native_artifact",
        metadata=metadata, vector=[0.6, 0.8],
    )

    assert decision.gap == "ENCODER_MISMATCH"


def test_changed_or_unproven_input_rejects_reuse() -> None:
    target = _target()
    metadata = _metadata(target)
    metadata["input_sha256"] = "b" * 64
    assert assess_embedding_reuse(
        target, source_kind="v1_native_artifact", metadata=metadata,
        vector=[1.0, 0.0],
    ).gap == "INPUT_MISMATCH"
    assert assess_embedding_reuse(
        target, source_kind="unknown", metadata=_metadata(target),
        vector=[1.0, 0.0],
    ).gap == "UNPROVEN_INPUT"
    metadata.pop("input_sha256")
    assert assess_embedding_reuse(
        target, source_kind="v1_native_artifact", metadata=metadata,
        vector=[1.0, 0.0],
    ).gap == "UNPROVEN_INPUT"


@pytest.mark.parametrize("vector", [[0.0, 0.0], [float("nan"), 1.0], [1.0, float("inf")], [1.0], [True, 1.0]])
def test_invalid_vector_is_never_reused(vector) -> None:
    target = _target()
    decision = assess_embedding_reuse(
        target, source_kind="v1_native_artifact",
        metadata=_metadata(target), vector=vector,
    )
    assert decision.gap == "INVALID_VECTOR"


def test_target_must_state_encoder_revision_and_video_sampling() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        replace(_target(), encoder_revision="")
    with pytest.raises(ValueError, match="fps and MIME"):
        replace(_target("video"), video_fps=None)
