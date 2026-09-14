from gateway.schemas import EmbeddingResult


def test_embedding_result_reports_vector_dimension() -> None:
    result = EmbeddingResult(
        model="test-model",
        modality="video",
        vector=[0.1, 0.2, 0.3],
    )

    assert result.dimension == 3
