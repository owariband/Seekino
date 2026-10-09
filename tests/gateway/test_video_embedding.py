from types import SimpleNamespace

import pytest

from gateway.ark.embedding import DoubaoMultimodalEmbedder
from gateway.usage import capture_model_usage


class FakeMultimodalEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            model=kwargs["model"],
            data=SimpleNamespace(embedding=[0.1, 0.2, 0.3],
                                 sparse_embedding=[SimpleNamespace(index=7, value=0.3)]),
            usage=SimpleNamespace(prompt_tokens=3, total_tokens=3),
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


def test_selected_model_supports_image_and_sparse_without_another_model(tmp_path) -> None:
    provider = FakeMultimodalEmbeddings()
    embedder = DoubaoMultimodalEmbedder("doubao-embedding-vision-250615",
                                      client=SimpleNamespace(multimodal_embeddings=provider))
    image = tmp_path / "prepared.png"
    image.write_bytes(b"prepared-image")
    calls = []
    with capture_model_usage(calls.append):
        dense = embedder.embed_image(image)
        sparse = embedder.embed_sparse("  formal text  ")
    image_call, sparse_call = provider.calls
    assert dense.model == sparse.model == "doubao-embedding-vision-250615"
    assert dense.modality == "image" and sparse.weights == {"7": 0.3}
    assert image_call["input"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert sparse_call["input"] == [{"type": "text", "text": "formal text"}]
    assert sparse_call["sparse_embedding"] == {"type": "enabled"}
    assert image_call["dimensions"] == sparse_call["dimensions"] == 1024
    assert [call.operation for call in calls] == ["image_embedding", "text_embedding"]
    assert all(call.usage.input_tokens == 3 and call.usage.output_tokens == 0 for call in calls)


@pytest.mark.parametrize("model", [
    "doubao-embedding-vision-250615",
    "doubao-embedding-vision-251215",
    "ep-user-configured",
    "custom-user-model",
])
@pytest.mark.parametrize("fps", [0.5, 2.0])
def test_video_embedding_sends_configured_model_and_fps(tmp_path, model, fps):
    provider = FakeMultimodalEmbeddings()
    embedder = DoubaoMultimodalEmbedder(model, fps=fps,
                                      client=SimpleNamespace(multimodal_embeddings=provider))
    video = tmp_path / "prepared.mp4"
    video.write_bytes(b"prepared-video")
    embedder.embed_video(video)
    request = provider.calls[0]
    assert request["model"] == model
    assert request["input"][0]["video_url"]["fps"] == fps


def test_provider_response_model_is_preserved_without_requested_model_substitution():
    provider = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
        model="actual-provider-model", data=SimpleNamespace(embedding=[1.0, 0.0]),
    ))
    embedder = DoubaoMultimodalEmbedder("requested-model",
                                      client=SimpleNamespace(multimodal_embeddings=provider))
    assert embedder.embed_text("query").model == "actual-provider-model"


@pytest.mark.parametrize("model", [None, "", " "])
def test_provider_must_report_actual_model(model):
    provider = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
        model=model, data=SimpleNamespace(embedding=[1.0, 0.0]),
    ))
    embedder = DoubaoMultimodalEmbedder("requested-model",
                                      client=SimpleNamespace(multimodal_embeddings=provider))
    with pytest.raises(RuntimeError, match="actual response model"):
        embedder.embed_text("query")


@pytest.mark.parametrize("entries,valid", [([], True), (None, False),
    ([SimpleNamespace(index=7, value=0.2), SimpleNamespace(index=7, value=0.3)], False),
    ([SimpleNamespace(index=True, value=0.2)], False),
])
def test_sparse_provider_response_distinguishes_empty_activations_from_invalid_entries(entries, valid):
    provider = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
        model="selected-model", data=SimpleNamespace(sparse_embedding=entries),
    ))
    embedder = DoubaoMultimodalEmbedder("selected-model",
                                      client=SimpleNamespace(multimodal_embeddings=provider))
    if valid:
        assert embedder.embed_sparse("query").weights == {}
    else:
        with pytest.raises(RuntimeError):
            embedder.embed_sparse("query")


def test_image_endpoint_rejects_gif_without_claiming_a_temporal_encoding_protocol(tmp_path):
    provider = FakeMultimodalEmbeddings()
    embedder = DoubaoMultimodalEmbedder("doubao-embedding-vision-250615",
                                      client=SimpleNamespace(multimodal_embeddings=provider))
    gif = tmp_path / "input.gif"
    gif.write_bytes(b"gif")
    with pytest.raises(ValueError, match="unsupported image file type"):
        embedder.embed_image(gif)
    assert provider.calls == []
