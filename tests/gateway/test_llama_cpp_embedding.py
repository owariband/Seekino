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
