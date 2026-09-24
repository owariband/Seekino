import json

import httpx
import pytest
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from gateway.errors import ContentFilterError
from gateway.multimodal import ImageInput, TextInput
from gateway.openai.audio import transcribe_audio
from gateway.openai.vision import OpenAIVisionModel


class Result(BaseModel):
    summary: str


def install_transport(monkeypatch, module, handler):
    clients = []

    def create_client(**kwargs):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(http)
        return OpenAI(**kwargs, http_client=http)

    monkeypatch.setattr(module + ".OpenAI", create_client)
    return clients


@pytest.mark.parametrize(
    "output_format,token_parameter",
    [("json_schema", "max_tokens"), ("json_object", "max_completion_tokens")],
)
def test_vision_sends_images_to_selected_endpoint_and_validates_json(
    monkeypatch, tmp_path, output_format, token_parameter
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "result",
                "object": "chat.completion",
                "created": 0,
                "model": "custom-vision",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"summary":"scene"}',
                        },
                    }
                ],
            },
        )

    clients = install_transport(monkeypatch, "gateway.openai.vision", handler)
    image = tmp_path / "frame.png"
    image.write_bytes(b"test-image")
    model = OpenAIVisionModel(
        "custom-vision",
        provider="custom",
        base_url="https://vision.example/v1",
        api_key="fake-test-key",
        token_parameter=token_parameter,
        response_format=output_format,
    )
    result = model.generate(
        [TextInput("describe"), ImageInput(image)],
        schema_name="scene",
        result_type=Result,
    )
    assert result.summary == "scene"
    request = requests[0]
    assert str(request.url) == "https://vision.example/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "custom-vision"
    assert body[token_parameter] == 8192
    assert body["response_format"]["type"] == output_format
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "reason,content,error",
    [
        ("length", "{}", RuntimeError),
        ("content_filter", "", ContentFilterError),
        ("stop", "{}", ValidationError),
    ],
)
def test_vision_rejects_incomplete_filtered_or_invalid_output(
    monkeypatch, reason, content, error
):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "result",
                "object": "chat.completion",
                "created": 0,
                "model": "vision",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": reason,
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            },
        )

    clients = install_transport(monkeypatch, "gateway.openai.vision", handler)
    model = OpenAIVisionModel(
        "vision",
        provider="custom",
        base_url="https://vision.example/v1",
        api_key="fake-test-key",
    )
    with pytest.raises(error):
        model.generate([TextInput("describe")], schema_name="scene", result_type=Result)
    assert all(client.is_closed for client in clients)


def test_audio_uses_transcriptions_multipart_protocol(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "task": "transcribe",
                "language": "zh",
                "duration": 1,
                "text": "你好",
                "segments": [{"start": 0, "end": 1, "text": "你好"}],
            },
        )

    clients = install_transport(monkeypatch, "gateway.openai.audio", handler)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake-wav")
    result = transcribe_audio(
        audio,
        model="third-party-asr",
        provider="speech",
        base_url="https://speech.example/v1",
        api_key="fake-test-key",
        timeout=15,
        response_format="verbose_json",
    )
    assert result["text"] == "你好"
    assert str(requests[0].url) == "https://speech.example/v1/audio/transcriptions"
    assert b"third-party-asr" in requests[0].content
    assert b"fake-wav" in requests[0].content
    assert all(client.is_closed for client in clients)
