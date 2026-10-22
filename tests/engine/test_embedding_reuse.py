from __future__ import annotations

from dataclasses import replace

import pytest

from engine.rag.encoding import (
    EmbeddingReuseDecision,
    EmbeddingReuseTarget,
    assess_embedding_reuse,
    embedding_receipt,
    validate_sparse_weights,
)

INPUT_SHA = "a" * 64


def _target(modality: str = "text") -> EmbeddingReuseTarget:
    common = {
        "modality": modality,
        "input_sha256": INPUT_SHA,
        "provider": "ark",
        "model": "endpoint-v1",
        "encoder_revision": "gateway-ark-embedding-v1",
        "dimensions": 2,
        "normalization": "none",
        "output_contract": "float-vector-v1",
        "encoder_id": f"{modality}:sha256:{'b' * 64}",
    }
    if modality == "video":
        common.update(
            input_contract_id=f"video_clip:sha256:{'c' * 64}",
            visual_input_kind="video_clip",
            video_fps=1.0,
            mime_type="video/mp4",
            max_height=720,
            preprocess_revision="ffmpeg-v1",
        )
    return EmbeddingReuseTarget(**common)


@pytest.mark.parametrize("modality", ["text", "video"])
def test_complete_matching_receipt_is_reused(modality: str) -> None:
    target = _target(modality)
    receipt = embedding_receipt(target, [0.6, 0.8])

    decision = assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=receipt["vector"],
    )

    assert decision == EmbeddingReuseDecision((0.6, 0.8), None)


@pytest.mark.parametrize("source_kind", ["v1_native_artifact", "import_parse_cache"])
@pytest.mark.parametrize("modality", ["text", "video"])
def test_retired_embedding_sources_are_not_reused(source_kind, modality) -> None:
    target = _target(modality)
    receipt = embedding_receipt(target, [0.6, 0.8])
    assert assess_embedding_reuse(
        target, source_kind=source_kind, metadata=receipt, vector=receipt["vector"],
    ) == EmbeddingReuseDecision(None, "UNPROVEN_INPUT")


def test_legacy_text_receipt_without_derived_encoder_id_is_reused() -> None:
    target = _target()
    receipt = embedding_receipt(target, [1.0, 0.0])
    receipt.pop("encoder_id")

    decision = assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=receipt["vector"],
    )

    assert decision == EmbeddingReuseDecision((1.0, 0.0), None)


@pytest.mark.parametrize(
    "missing",
    [
        "provider",
        "model",
        "encoder_revision",
        "dimensions",
        "normalization",
        "output_contract",
    ],
)
def test_legacy_text_receipt_still_requires_every_encoder_claim(
    missing: str,
) -> None:
    target = _target()
    receipt = embedding_receipt(target, [1.0, 0.0])
    receipt.pop("encoder_id")
    receipt.pop(missing)

    decision = assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=receipt["vector"],
    )

    assert decision == EmbeddingReuseDecision(None, "UNPROVEN_ENCODER")


def test_legacy_text_receipt_with_wrong_derived_encoder_id_is_rejected() -> None:
    target = _target()
    receipt = embedding_receipt(target, [1.0, 0.0])
    receipt["encoder_id"] = "text:sha256:" + "c" * 64

    decision = assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=receipt["vector"],
    )

    assert decision == EmbeddingReuseDecision(None, "ENCODER_MISMATCH")


@pytest.mark.parametrize(
    "missing",
    [
        "provider",
        "model",
        "encoder_revision",
        "dimensions",
        "normalization",
        "output_contract",
        "encoder_id",
        "input_contract_id",
        "visual_input_kind",
        "video_fps",
        "mime_type",
        "max_height",
        "preprocess_revision",
    ],
)
def test_missing_visual_encoder_or_preprocess_claim_rejects_reuse(
    missing: str,
) -> None:
    target = _target("video")
    receipt = embedding_receipt(target, [0.6, 0.8])
    receipt.pop(missing)

    decision = assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=[0.6, 0.8],
    )

    assert decision == EmbeddingReuseDecision(None, "UNPROVEN_ENCODER")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("encoder_id", "video:sha256:" + "d" * 64),
        ("input_contract_id", "video_clip:sha256:" + "e" * 64),
        ("video_fps", 2.0),
        ("max_height", 1080),
        ("preprocess_revision", "ffmpeg-v2"),
    ],
)
def test_changed_visual_encoding_contract_rejects_reuse(
    field: str,
    value: object,
) -> None:
    target = _target("video")
    receipt = embedding_receipt(target, [0.6, 0.8])
    receipt[field] = value

    decision = assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=[0.6, 0.8],
    )

    assert decision == EmbeddingReuseDecision(None, "ENCODER_MISMATCH")


def test_changed_or_unproven_input_rejects_reuse() -> None:
    target = _target()
    receipt = embedding_receipt(target, [1.0, 0.0])
    receipt["input_sha256"] = "f" * 64
    assert assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=[1.0, 0.0],
    ).gap == "INPUT_MISMATCH"
    assert assess_embedding_reuse(
        target,
        source_kind="unknown",
        metadata=embedding_receipt(target, [1.0, 0.0]),
        vector=[1.0, 0.0],
    ).gap == "UNPROVEN_INPUT"


@pytest.mark.parametrize(
    "vector",
    [
        [0.0, 0.0],
        [float("nan"), 1.0],
        [1.0, float("inf")],
        [1.0],
        [True, 1.0],
    ],
)
def test_invalid_vector_is_never_reused(vector: list[float]) -> None:
    target = _target()
    receipt = embedding_receipt(target, [1.0, 0.0])

    decision = assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=receipt,
        vector=vector,
    )

    assert decision == EmbeddingReuseDecision(None, "INVALID_VECTOR")


def test_visual_target_requires_complete_preprocess_identity() -> None:
    with pytest.raises(ValueError, match="max_height"):
        replace(_target("video"), max_height=None)
    with pytest.raises(ValueError, match="preprocess"):
        replace(_target("video"), preprocess_revision=None)


def test_sparse_weights_are_sorted_copied_and_keep_small_positive_values() -> None:
    weights = {"scene": 2, "quiet": 1e-12, "千早爱音": 0.5, "zero": 0.0}

    validated = validate_sparse_weights(weights)

    assert validated == (("quiet", 1e-12), ("scene", 2.0), ("千早爱音", 0.5))
    assert weights["zero"] == 0.0
    weights["scene"] = 9
    assert validated[1] == ("scene", 2.0)


@pytest.mark.parametrize("weights", [{}, {"zero": 0}, {"zero": -0.0}])
def test_sparse_empty_or_zero_only_weights_are_valid(weights: dict[str, float]) -> None:
    assert validate_sparse_weights(weights) == ()


@pytest.mark.parametrize("token", ["", " ", 1, None])
def test_sparse_weights_require_nonblank_string_tokens(token: object) -> None:
    with pytest.raises(ValueError):
        validate_sparse_weights({token: 0.0})


@pytest.mark.parametrize(
    "weight",
    [
        True,
        False,
        -0.1,
        float("nan"),
        float("inf"),
        float("-inf"),
        10**1000,
        "1.0",
        None,
    ],
)
def test_sparse_weights_require_finite_nonnegative_numeric_values(weight: object) -> None:
    with pytest.raises(ValueError):
        validate_sparse_weights({"scene": weight})
