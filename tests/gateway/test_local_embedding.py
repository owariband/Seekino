from pathlib import Path
from contextlib import closing
import json

import httpx
import pytest
import yaml
from pydantic import ValidationError

from gateway.config import GatewayConfig, resolve_source_api_key
from gateway.local.embedding import (
    OllamaEmbeddingError, OllamaTextEmbedder, ollama_encoder_revision,
)
from gateway.model_factory import GatewayClient
from gateway.multimodal import AdapterCapabilities, adapter_capabilities
from gateway.usage import capture_model_usage, control_model_calls, ModelCallCancelledError


MODEL = "qwen3-embedding:0.6b"


def test_sparse_embedding_requires_a_declared_adapter_capability():
    assert AdapterCapabilities().sparse_embedding is False
    assert adapter_capabilities("ark").sparse_embedding is True
    assert adapter_capabilities("ollama_embedding").embedding is True
    assert adapter_capabilities("ollama_embedding").sparse_embedding is False


def local_config():
    data = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text())
    data["model_sources"]["local_embedding"] = {
        "adapter": "ollama_embedding", "provider": "ollama", "base_url": "http://127.0.0.1:11434",
    }
    data["models"]["embedding"] = {
        "kind": "embedding", "source": "local_embedding", "model": MODEL,
        "input_mode": "text", "dimensions": 1024, "video_fps": 1,
        "query_instruction": "Retrieve course passages relevant to the question.",
        "request_timeout_seconds": 23,
    }
    return data


def make_embedder(payload=None, *, handler=None, instruction=None):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if handler:
            return handler(request)
        return httpx.Response(200, json=payload or {
            "model": MODEL, "embeddings": [[3.0, 4.0]], "prompt_eval_count": 7,
        })

    client = httpx.Client(transport=httpx.MockTransport(respond))
    embedder = OllamaTextEmbedder(
        MODEL, base_url="http://127.0.0.1:11434", dimensions=2,
        query_instruction=instruction, client=client,
    )
    return embedder, calls


def test_query_instruction_is_asymmetric_and_output_is_normalized():
    embedder, requests = make_embedder(instruction="Find relevant course passages.")
    calls = []
    with capture_model_usage(calls.append):
        document = embedder.embed_text("  Chain rule computes the gradient.  ")
        query = embedder.embed_query("  链式法则如何求导？  ")
    assert document.vector == query.vector == [0.6, 0.8]
    assert document.model == MODEL and document.modality == "text"
    assert requests == [
        {"model": MODEL, "input": ["Chain rule computes the gradient."], "dimensions": 2, "truncate": False},
        {"model": MODEL, "input": ["Instruct: Find relevant course passages.\nQuery: 链式法则如何求导？"],
         "dimensions": 2, "truncate": False},
    ]
    assert all(call.provider == "ollama" and call.operation == "text_embedding" for call in calls)
    assert all(call.usage.input_tokens == 7 and call.usage.total_tokens == 7 for call in calls)
    assert all(call.usage.output_tokens == 0 and call.status == "completed" for call in calls)
    embedder.close()


@pytest.mark.parametrize("payload,message", [
    ([], "envelope"),
    ({"model": "other", "embeddings": [[1, 0]]}, "model differs"),
    ({"embeddings": [[1, 0]]}, "model differs"),
    ({"model": MODEL, "embeddings": []}, "exactly one"),
    ({"model": MODEL, "embeddings": [[1, 0], [0, 1]]}, "exactly one"),
    ({"model": MODEL, "embeddings": [[1]]}, "dimensions"),
    ({"model": MODEL, "embeddings": [[True, 1]]}, "finite"),
    ({"model": MODEL, "embeddings": [["1", 1]]}, "finite"),
    ({"model": MODEL, "embeddings": [[0, 0]]}, "zero"),
])
def test_invalid_provider_output_fails_instead_of_indexing_it(payload, message):
    embedder, _ = make_embedder(handler=lambda _: httpx.Response(200, json=payload))
    calls = []
    with capture_model_usage(calls.append), pytest.raises(OllamaEmbeddingError, match=message):
        embedder.embed_text("formal text")
    assert calls[0].status == "failed"
    embedder.close()


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_response_is_rejected(value):
    body = f'{{"model": "{MODEL}", "embeddings": [[{value}, 1]]}}'
    embedder, _ = make_embedder(handler=lambda _: httpx.Response(200, text=body))
    with pytest.raises(OllamaEmbeddingError, match="finite"):
        embedder.embed_text("formal text")
    embedder.close()


def test_http_errors_are_not_retried_or_replaced_by_fake_vectors():
    embedder, requests = make_embedder(handler=lambda _: httpx.Response(400, json={"error": "input too long"}))
    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed_text("long input")
    assert len(requests) == 1 and requests[0]["truncate"] is False
    embedder.close()


def test_non_json_is_reported_as_an_invalid_embedding_response():
    embedder, _ = make_embedder(handler=lambda _: httpx.Response(200, text="not JSON"))
    with pytest.raises(OllamaEmbeddingError, match="JSON"):
        embedder.embed_text("formal text")
    embedder.close()


def test_missing_usage_stays_unknown():
    embedder, _ = make_embedder({"model": MODEL, "embeddings": [[1, 0]]})
    calls = []
    with capture_model_usage(calls.append):
        embedder.embed_text("formal text")
    assert calls[0].usage is None
    embedder.close()


def test_unsupported_modalities_and_empty_text_never_call_the_server():
    embedder, requests = make_embedder()
    for method, value in [(embedder.embed_image, "frame.png"), (embedder.embed_video, "clip.mp4"),
                          (embedder.embed_sparse, "text"), (embedder.embed_text, " "),
                          (embedder.embed_query, " ")]:
        with pytest.raises(ValueError):
            method(value)
    assert requests == []
    embedder.close()


def test_encoder_revision_changes_with_query_encoding_contract():
    assert ollama_encoder_revision("Find courses.") == ollama_encoder_revision("Find courses.")
    assert len({ollama_encoder_revision(None), ollama_encoder_revision("Find courses."),
                ollama_encoder_revision("Find images.")}) == 3


def test_local_binding_requires_no_key_and_preserves_frozen_parameters():
    config = GatewayConfig.model_validate(local_config())
    source = config.source("local_embedding")
    assert resolve_source_api_key(source, environ={}) is None
    binding = config.resolve_embedding_binding(environ={})
    assert binding.input_mode == "text" and binding.dimensions == 1024
    assert binding.query_instruction == "Retrieve course passages relevant to the question."
    assert config.resolve_embedding_binding(binding, environ={"SEEKINO_VIDEO_EMBEDDING_MODEL": "other"}) == binding
    assert GatewayConfig.model_validate(config.model_dump()).resolve_embedding_binding(environ={}) == binding
    with closing(GatewayClient(config, environ={})) as client:
        info = client.describe_embedding(binding)
        assert info.provider_name == "ollama" and info.encoder_model == MODEL
        assert info.encoder_revision == ollama_encoder_revision(binding.query_instruction)


@pytest.mark.parametrize("url", ["http://remote.example:11434", "http://localhost:11434?key=secret",
                                 "http://user:password@127.0.0.1:11434"])
def test_local_source_rejects_unprotected_remote_http_or_url_credentials(url):
    data = local_config()
    data["model_sources"]["local_embedding"]["base_url"] = url
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(data)


def test_local_adapter_requires_explicit_text_mode_and_rejects_generative_use():
    data = local_config()
    data["models"]["embedding"].pop("input_mode")
    with pytest.raises(ValidationError, match="requires input_mode=text"):
        GatewayConfig.model_validate(data)
    data = local_config()
    data["models"]["vision"].update(source="local_embedding", model=MODEL)
    with pytest.raises(ValidationError, match="does not support adapter"):
        GatewayConfig.model_validate(data)


def test_ark_does_not_silently_ignore_a_query_instruction():
    data = local_config()
    data["models"]["embedding"]["source"] = "ark_default"
    with pytest.raises(ValidationError, match="query_instruction"):
        GatewayConfig.model_validate(data)


def test_gateway_factory_owns_local_client_records_source_and_forwards_timeout(monkeypatch):
    requests = []
    actual_client = httpx.Client(transport=httpx.MockTransport(lambda request: (
        requests.append(json.loads(request.content)) or httpx.Response(200, json={
            "model": MODEL, "embeddings": [[1.0] + [0.0] * 1023], "prompt_eval_count": 3,
        })
    )))
    options = []
    monkeypatch.setattr("gateway.local.embedding.httpx.Client", lambda **kwargs: options.append(kwargs) or actual_client)
    calls = []
    with closing(GatewayClient(GatewayConfig.model_validate(local_config()), environ={})) as gateway:
        with capture_model_usage(calls.append):
            model = gateway.create_embedding_model()
            result = model.embed_query("反向传播是什么？")
        assert len(result.vector) == 1024
        assert requests[0]["input"][0].startswith("Instruct: Retrieve course passages")
        assert calls[0].context["src"] == "local_embedding"
    assert actual_client.is_closed
    assert options == [{"timeout": 23.0, "trust_env": False}]
    with pytest.raises(RuntimeError, match="closed"):
        model.embed_text("after close")


def test_cancellation_after_response_rejects_result_and_closes_transport():
    trackers = []

    class Control:
        def acquire(self, tracker):
            trackers.append(tracker)

        def release(self, tracker):
            pass

    def cancel(request):
        trackers[0].cancel()
        return httpx.Response(200, json={"model": MODEL, "embeddings": [[1, 0]]})

    embedder, _ = make_embedder(handler=cancel)
    with control_model_calls(Control()), pytest.raises(ModelCallCancelledError):
        embedder.embed_text("formal text")
    assert embedder._client.is_closed
