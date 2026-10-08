import pytest
from pydantic import ValidationError

from gateway.embedding import EmbeddingResult, SparseEmbeddingResult


def test_embedding_result_reports_vector_dimension() -> None:
    result = EmbeddingResult(
        model="test-model",
        modality="video",
        vector=[0.1, 0.2, 0.3],
    )

    assert result.dimension == 3


def test_image_embedding_result_and_sparse_weights_are_provider_neutral() -> None:
    image = EmbeddingResult(model="selected", modality="image", vector=[1.0, 0.0])
    sparse = SparseEmbeddingResult(model="selected", weights={"7": 0.2, "42": 0.0})
    assert image.modality == "image" and image.dimension == 2
    assert sparse.model_dump() == {"model": "selected", "weights": {"7": 0.2, "42": 0.0}}
    assert SparseEmbeddingResult(model="selected", weights={}).weights == {}


@pytest.mark.parametrize("weight", [True, "0.2", -1.0, float("inf"), float("nan")])
def test_sparse_result_rejects_invalid_provider_weights(weight) -> None:
    with pytest.raises(ValidationError):
        SparseEmbeddingResult(model="selected", weights={"7": weight})
