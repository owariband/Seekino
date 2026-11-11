import json
from pathlib import Path

import httpx
import pytest
import yaml

from gateway.config import GatewayConfig, resolve_source_api_key
from gateway.embedding import build_embedding_model, describe_embedding_model
from gateway.local.llama_cpp_embedding import LlamaCppTextEmbedder, LlamaCppEmbeddingError
from gateway.usage import capture_model_usage

MODEL = 'course-qwen'


def configured():
    data = yaml.safe_load(Path('config.yaml').read_text())
    data['model_sources']['course'] = {'adapter': 'llama_cpp_embedding', 'provider': 'llama_cpp', 'base_url': 'http://127.0.0.1:18843'}
    data['models']['embedding'] = {'source': 'course', 'model': MODEL, 'dimensions': 1024, 'video_fps': 1, 'input_mode': 'text', 'query_instruction': 'Find course passages.'}
    return data


def test_gateway_freezes_llama_contract_without_credentials():
    config = GatewayConfig.model_validate(configured())
    binding = config.resolve_embedding_binding(environ={})
    source = config.source(binding.source)
    assert resolve_source_api_key(source, environ={}) is None
    info = describe_embedding_model(binding, source)
    assert info.provider_name == 'llama_cpp'
    assert 'llama-cpp' in info.encoder_revision
    changed = binding.model_copy(update={'query_instruction': 'Different task.'})
    assert describe_embedding_model(changed, source).encoder_revision != info.encoder_revision
    model = build_embedding_model(binding, source, api_key=None)
    assert model.dimensions == 1024
    model.close()
    with pytest.raises(RuntimeError, match='closed'):
        model.embed_text('closed')


def test_text_mode_is_required():
    data = configured()
    data['models']['embedding']['input_mode'] = 'multimodal'
    with pytest.raises(ValueError, match='requires input_mode=text'):
        GatewayConfig.model_validate(data)


def test_document_query_protocol_normalization_and_real_usage():
    requests = []
    def respond(request):
        assert request.url.path == '/v1/embeddings'
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'model': MODEL, 'data': [{'index': 0, 'embedding': [3, 4]}], 'usage': {'prompt_tokens': 7, 'total_tokens': 7}})
    model = LlamaCppTextEmbedder(MODEL, base_url='http://127.0.0.1:18843', dimensions=2, query_instruction='Find passages.', client=httpx.Client(transport=httpx.MockTransport(respond)))
    usage = []
    with capture_model_usage(usage.append):
        assert model.embed_text('Chain rule').vector == [0.6, 0.8]
        assert model.embed_query('链式法则').vector == [0.6, 0.8]
    assert requests[0]['input'] == ['Chain rule']
    assert requests[1]['input'] == ['Instruct: Find passages.\nQuery: 链式法则']
    assert all(r['encoding_format'] == 'float' and r['truncate'] is False for r in requests)
    assert all(u.provider == 'llama_cpp' and u.usage.input_tokens == 7 for u in usage)
    with pytest.raises(ValueError, match='only supports text'): model.embed_image('image.png')
    model.close()


@pytest.mark.parametrize('payload', [
    {'model': 'wrong', 'data': [{'index': 0, 'embedding': [1, 0]}]},
    {'model': MODEL, 'data': []},
    {'model': MODEL, 'data': [{'index': 1, 'embedding': [1, 0]}]},
    {'model': MODEL, 'data': [{'index': 0, 'embedding': [[1, 0]]}]},
    {'model': MODEL, 'data': [{'index': 0, 'embedding': [0, 0]}]},
    {'model': MODEL, 'data': [{'index': 0, 'embedding': [True, 0]}]},
])
def test_invalid_vectors_never_enter_index(payload):
    model = LlamaCppTextEmbedder(MODEL, base_url='http://127.0.0.1:18843', dimensions=2, client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))))
    with pytest.raises(LlamaCppEmbeddingError): model.embed_text('formal text')
    model.close()


@pytest.mark.parametrize("operation, primary, expected, error", [
    ("query", 200, [1], None),
    ("query", 503, [1], httpx.HTTPStatusError),
    ("text", 200, [2], None),
    ("text", "refused", [2, 1], None),
    ("text", 503, [2, 1], None),
    ("text", 429, [2, 1], None),
    ("text", 401, [2], httpx.HTTPStatusError),
    ("text", "wrong model", [2], LlamaCppEmbeddingError),
    ("text", "wrong shape", [2], LlamaCppEmbeddingError),
])
def test_embedding_replica_order(operation, primary, expected, error):
    ports, bodies = [], []
    def respond(request):
        ports.append(request.url.port)
        bodies.append(json.loads(request.content))
        result = primary if len(ports) == 1 else 200
        if result == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(result if isinstance(result, int) else 200, json={
            "model": "unexpected" if result == "wrong model" else MODEL,
            "data": [{"index": 0, "embedding": [3] if result == "wrong shape" else [3, 4]}],
        })

    urls = ("http://127.0.0.1:2", "http://127.0.0.1:1")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = LlamaCppTextEmbedder(
            MODEL, base_url=urls[1], document_base_urls=urls, dimensions=2,
            query_instruction="Find clips.", client=client,
        )
        encode = model.embed_query if operation == "query" else model.embed_text
        text = "Instruct: quoted in a document"
        if error:
            with pytest.raises(error):
                encode(text)
        else:
            assert encode(text).vector == [0.6, 0.8]
    assert ports == expected
    assert all(body == bodies[0] for body in bodies)
    expected_text = "Instruct: Find clips.\nQuery: " + text if operation == "query" else text
    assert bodies[0]["input"] == [expected_text]


def test_document_replicas_keep_index_identity_and_validate_every_url():
    data = configured()
    original = GatewayConfig.model_validate(data)
    binding = original.resolve_embedding_binding(environ={})
    routes = data["model_sources"]["course"]
    routes["document_base_urls"] = ["https://replica.example", routes["base_url"]]
    source = GatewayConfig.model_validate(data).source(binding.source)
    assert describe_embedding_model(binding, source) == describe_embedding_model(binding, original.source(binding.source))
    routes["document_base_urls"] = ["http://plain-http.example"]
    with pytest.raises(ValueError):
        GatewayConfig.model_validate(data)
