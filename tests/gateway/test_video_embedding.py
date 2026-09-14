from types import SimpleNamespace

import pytest

from gateway.multimodal_embedder import DoubaoMultimodalEmbedder


class FakeMultimodalEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=SimpleNamespace(embedding=[0.1, 0.2, 0.3])
        )


def test_video_text_and_query_use_the_same_embedding_model(tmp_path) -> None:
    multimodal_embeddings = FakeMultimodalEmbeddings()
    client = SimpleNamespace(multimodal_embeddings=multimodal_embeddings)
    embedder = DoubaoMultimodalEmbedder(
        "doubao-embedding-vision-251215",
        output_dimensionality=1024,
        fps=2.0,
        client=client,
    )
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"fake-video")

    video_result = embedder.embed_video(video_path)
    text_result = embedder.embed_text("  一段时间线文本  ")
    query_result = embedder.embed_query("惊世一跪")

    assert video_result.model == text_result.model == query_result.model
    assert video_result.model == "doubao-embedding-vision-251215"
    assert video_result.modality == "video"
    assert text_result.modality == "text"
    assert query_result.modality == "text"
    video_call, text_call, query_call = multimodal_embeddings.calls
    assert video_call["model"] == query_call["model"]
    assert video_call["input"][0]["type"] == "video_url"
    video_url = video_call["input"][0]["video_url"]
    assert video_url["url"].startswith("data:video/mp4;base64,")
    assert video_url["fps"] == 2.0
    assert text_call["input"] == [{"type": "text", "text": "一段时间线文本"}]
    assert query_call["input"] == [{"type": "text", "text": "惊世一跪"}]
    assert (
        video_call["dimensions"]
        == text_call["dimensions"]
        == query_call["dimensions"]
        == 1024
    )


def test_embedding_dimension_must_be_supported() -> None:
    with pytest.raises(ValueError, match="between 1 and 2048"):
        DoubaoMultimodalEmbedder(
            "doubao-embedding-vision-251215",
            output_dimensionality=4096,
            client=SimpleNamespace(),
        )
