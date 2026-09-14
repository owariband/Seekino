import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from gateway.models import (
    ArkMultimodalModel,
    DeepSeekModelError,
    DeepSeekMultimodalModel,
    ImageInput,
    LlamaCppModelError,
    LlamaCppMultimodalModel,
    OllamaModelError,
    OllamaMultimodalModel,
    TextInput,
    VideoInput,
)


class Scene(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp_ms: int
    description: str


class StructuredResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    scenes: list[Scene]


def _result() -> StructuredResult:
    return StructuredResult(
        summary="两帧动画画面",
        scenes=[
            Scene(timestamp_ms=1000, description="人物站在房间内"),
            Scene(timestamp_ms=3000, description="人物转身看向门口"),
        ],
    )


class FakeStream:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self) -> None:
        self.closed = True


class FakeResponses:
    def __init__(self, result: StructuredResult) -> None:
        self.calls: list[dict[str, object]] = []
        response_text = result.model_dump_json()
        response = SimpleNamespace(
            id="response-ark-test",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(type="output_text", text=response_text)
                    ],
                )
            ],
        )
        self.stream = FakeStream(
            [
                SimpleNamespace(
                    type="response.completed",
                    sequence_number=0,
                    response=response,
                )
            ]
        )

    def create(self, **kwargs: object) -> FakeStream:
        self.calls.append(kwargs)
        return self.stream


def test_ark_maps_text_image_and_video_to_responses_sse(tmp_path) -> None:
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"image-bytes")
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"video-bytes")
    responses = FakeResponses(_result())
    model = ArkMultimodalModel(
        "ark-test-model",
        client=SimpleNamespace(responses=responses),
    )

    result = model.generate(
        [
            TextInput("按顺序分析以下媒体"),
            ImageInput(image_path, detail="low"),
            VideoInput(video_path, fps=0.5),
        ],
        schema_name="structured_result",
        result_type=StructuredResult,
    )

    assert result == _result()
    assert responses.stream.closed is True
    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call["stream"] is True
    assert call["model"] == "ark-test-model"
    content = call["input"][0]["content"]
    assert content[0] == {
        "type": "input_text",
        "text": "按顺序分析以下媒体",
    }
    assert content[1]["type"] == "input_image"
    assert content[1]["detail"] == "low"
    assert content[1]["image_url"] == (
        "data:image/jpeg;base64,"
        + base64.b64encode(b"image-bytes").decode("ascii")
    )
    assert content[2]["type"] == "input_video"
    assert content[2]["fps"] == 0.5
    assert content[2]["video_url"] == (
        "data:video/mp4;base64,"
        + base64.b64encode(b"video-bytes").decode("ascii")
    )
    assert call["text"]["format"] == {
        "type": "json_schema",
        "name": "structured_result",
        "schema": StructuredResult.model_json_schema(),
        "strict": True,
    }


class FakeDeepSeekResponses:
    def __init__(self, output_text: str, *, status: str = "completed") -> None:
        self.calls: list[dict[str, object]] = []
        self.output_text = output_text
        self.status = status

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            status=self.status,
            output_text=self.output_text,
        )


def test_deepseek_uses_official_responses_api_and_preserves_ordered_images(
    tmp_path,
) -> None:
    first_image = tmp_path / "first.jpg"
    first_image.write_bytes(b"first-image")
    second_image = tmp_path / "second.png"
    second_image.write_bytes(b"second-image")
    responses = FakeDeepSeekResponses(_result().model_dump_json())
    model = DeepSeekMultimodalModel(
        "deepseek-flash",
        base_url="https://api.deepseek.com/",
        api_key="test-key",
        max_output_tokens=777,
        request_timeout_seconds=12,
        reasoning_effort="none",
        client=SimpleNamespace(responses=responses),
    )

    result = model.generate(
        [
            TextInput("Frame 1: t=1000 ms"),
            ImageInput(first_image),
            TextInput("Frame 2: t=3000 ms"),
            ImageInput(second_image, detail="low"),
            TextInput("严格按时间顺序返回"),
        ],
        schema_name="structured_result",
        result_type=StructuredResult,
    )

    assert result == _result()
    assert len(responses.calls) == 1
    call = responses.calls[0]
    assert call["model"] == "deepseek-flash"
    assert call["reasoning"] == {"effort": "none"}
    assert call["temperature"] == 0
    assert call["max_output_tokens"] == 777
    content = call["input"][0]["content"]
    assert [item["type"] for item in content] == [
        "input_text",
        "input_image",
        "input_text",
        "input_image",
        "input_text",
    ]
    assert content[1] == {
        "type": "input_image",
        "image_url": (
            "data:image/jpeg;base64,"
            + base64.b64encode(b"first-image").decode("ascii")
        ),
        "detail": "high",
    }
    assert content[3] == {
        "type": "input_image",
        "image_url": (
            "data:image/png;base64,"
            + base64.b64encode(b"second-image").decode("ascii")
        ),
        "detail": "low",
    }
    response_format = call["text"]["format"]
    assert response_format["type"] == "json_schema"
    assert response_format["name"] == "structured_result"
    assert response_format["strict"] is True
    rendered_schema = json.dumps(response_format["schema"])
    assert '"$defs"' not in rendered_schema
    assert '"$ref"' not in rendered_schema


def test_deepseek_rejects_video_without_sending_a_request(tmp_path) -> None:
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"video")
    responses = FakeDeepSeekResponses(_result().model_dump_json())
    model = DeepSeekMultimodalModel(
        "deepseek-flash",
        base_url="https://api.deepseek.com",
        api_key="test-key",
        max_output_tokens=100,
        request_timeout_seconds=10,
        client=SimpleNamespace(responses=responses),
    )

    with pytest.raises(ValueError, match="does not support VideoInput"):
        model.generate(
            [VideoInput(video_path, fps=1.0)],
            schema_name="structured_result",
            result_type=StructuredResult,
        )
    assert responses.calls == []


@pytest.mark.parametrize(
    ("output_text", "status", "message"),
    [
        ("", "completed", "returned no output text"),
        (
            _result().model_dump_json(),
            "incomplete",
            "exhausted max_output_tokens",
        ),
        ('{"summary":"missing scenes"}', "completed", "does not match schema"),
    ],
)
def test_deepseek_rejects_invalid_structured_responses(
    output_text: str,
    status: str,
    message: str,
) -> None:
    responses = FakeDeepSeekResponses(output_text, status=status)
    model = DeepSeekMultimodalModel(
        "deepseek-flash",
        base_url="https://api.deepseek.com",
        api_key="test-key",
        max_output_tokens=100,
        request_timeout_seconds=10,
        client=SimpleNamespace(responses=responses),
    )

    with pytest.raises(DeepSeekModelError, match=message):
        model.generate(
            [TextInput("describe")],
            schema_name="structured_result",
            result_type=StructuredResult,
        )


def test_ollama_uses_configured_url_and_preserves_ordered_images(tmp_path) -> None:
    first_image = tmp_path / "first.jpg"
    first_image.write_bytes(b"first-image")
    second_image = tmp_path / "second.png"
    second_image.write_bytes(b"second-image")
    requests: list[tuple[str, dict[str, object]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((str(request.url), payload))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": _result().model_dump_json()},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    model = OllamaMultimodalModel(
        "ollama-test-model",
        base_url="http://ollama.test/custom-root/",
        max_output_tokens=777,
        request_timeout_seconds=12,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    result = model.generate(
        [
            TextInput("Frame 1: t=1000 ms"),
            ImageInput(first_image),
            TextInput("Frame 2: t=3000 ms"),
            ImageInput(second_image, detail="low"),
            TextInput("严格按时间顺序返回"),
        ],
        schema_name="structured_result",
        result_type=StructuredResult,
    )

    assert result == _result()
    assert len(requests) == 1
    request_url, payload = requests[0]
    assert request_url == "http://ollama.test/custom-root/v1/chat/completions"
    assert payload["model"] == "ollama-test-model"
    assert payload["temperature"] == 0
    assert payload["max_tokens"] == 777
    assert payload["stream"] is False

    content = payload["messages"][0]["content"]
    assert [item["type"] for item in content] == [
        "text",
        "image_url",
        "text",
        "image_url",
        "text",
    ]
    assert content[0]["text"] == "Frame 1: t=1000 ms"
    assert content[1]["image_url"]["url"] == (
        "data:image/jpeg;base64,"
        + base64.b64encode(b"first-image").decode("ascii")
    )
    assert content[3]["image_url"] == {
        "url": (
            "data:image/png;base64,"
            + base64.b64encode(b"second-image").decode("ascii")
        )
    }
    assert content[4]["text"] == "严格按时间顺序返回"

    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "structured_result"
    assert response_format["json_schema"]["strict"] is True
    rendered_schema = json.dumps(response_format["json_schema"]["schema"])
    assert '"$defs"' not in rendered_schema
    assert '"$ref"' not in rendered_schema


def test_ollama_rejects_video_without_sending_a_request(tmp_path) -> None:
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"video")

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected Ollama request: {request.url}")

    model = OllamaMultimodalModel(
        "ollama-test-model",
        base_url="http://ollama.test",
        max_output_tokens=100,
        request_timeout_seconds=10,
        client=httpx.Client(transport=httpx.MockTransport(fail_if_called)),
    )

    with pytest.raises(ValueError, match="does not support VideoInput"):
        model.generate(
            [VideoInput(video_path, fps=1.0)],
            schema_name="structured_result",
            result_type=StructuredResult,
        )


def test_ollama_wraps_http_errors() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    model = OllamaMultimodalModel(
        "ollama-test-model",
        base_url="http://ollama.test",
        max_output_tokens=100,
        request_timeout_seconds=10,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(OllamaModelError, match="request failed"):
        model.generate(
            [TextInput("describe")],
            schema_name="structured_result",
            result_type=StructuredResult,
        )


@pytest.mark.parametrize(
    ("completion", "message"),
    [
        ({"choices": []}, "invalid chat completion envelope"),
        (
            {
                "choices": [
                    {
                        "message": {"content": _result().model_dump_json()},
                        "finish_reason": "length",
                    }
                ]
            },
            "exhausted max_output_tokens",
        ),
        (
            {
                "choices": [
                    {
                        "message": {"content": '{"summary":"missing scenes"}'},
                        "finish_reason": "stop",
                    }
                ]
            },
            "does not match schema structured_result",
        ),
    ],
)
def test_ollama_rejects_invalid_structured_responses(
    completion: dict[str, object],
    message: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion)

    model = OllamaMultimodalModel(
        "ollama-test-model",
        base_url="http://ollama.test",
        max_output_tokens=100,
        request_timeout_seconds=10,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(OllamaModelError, match=message):
        model.generate(
            [TextInput("describe")],
            schema_name="structured_result",
            result_type=StructuredResult,
        )


def test_llama_cpp_maps_text_image_and_video_in_input_order(tmp_path) -> None:
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"image-bytes")
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"video-bytes")
    requests: list[tuple[str, dict[str, object]]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((str(request.url), payload))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": _result().model_dump_json()},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    model = LlamaCppMultimodalModel(
        "llama-cpp-test-model",
        base_url="http://llama-cpp.test/custom-root/",
        max_output_tokens=888,
        request_timeout_seconds=15,
        expected_server_video_fps=0.5,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    result = model.generate(
        [
            TextInput("先看图片"),
            ImageInput(image_path),
            TextInput("再看视频"),
            VideoInput(video_path, fps=0.5),
            TextInput("严格按时间顺序返回"),
        ],
        schema_name="structured_result",
        result_type=StructuredResult,
    )

    assert result == _result()
    assert len(requests) == 1
    request_url, payload = requests[0]
    assert request_url == "http://llama-cpp.test/custom-root/v1/chat/completions"
    assert payload["model"] == "llama-cpp-test-model"
    assert payload["temperature"] == 0
    assert payload["max_tokens"] == 888
    assert payload["stream"] is False

    content = payload["messages"][0]["content"]
    assert [item["type"] for item in content] == [
        "text",
        "image_url",
        "text",
        "input_video",
        "text",
    ]
    assert content[0]["text"] == "先看图片"
    assert content[1]["image_url"]["url"] == (
        "data:image/jpeg;base64,"
        + base64.b64encode(b"image-bytes").decode("ascii")
    )
    assert content[2]["text"] == "再看视频"
    assert content[3]["input_video"] == {
        "data": base64.b64encode(b"video-bytes").decode("ascii")
    }
    assert not content[3]["input_video"]["data"].startswith("data:")
    assert content[4]["text"] == "严格按时间顺序返回"

    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert set(response_format) == {"type", "json_schema"}
    schema_wrapper = response_format["json_schema"]
    assert schema_wrapper["name"] == "structured_result"
    assert schema_wrapper["strict"] is True
    rendered_schema = json.dumps(schema_wrapper["schema"])
    assert '"$defs"' not in rendered_schema
    assert '"$ref"' not in rendered_schema


def test_llama_cpp_rejects_video_fps_mismatch_without_sending_request(
    tmp_path,
) -> None:
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"video")

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected llama.cpp request: {request.url}")

    model = LlamaCppMultimodalModel(
        "llama-cpp-test-model",
        base_url="http://llama-cpp.test",
        max_output_tokens=100,
        request_timeout_seconds=10,
        expected_server_video_fps=0.5,
        client=httpx.Client(transport=httpx.MockTransport(fail_if_called)),
    )

    with pytest.raises(
        ValueError,
        match="does not match the declared llama.cpp server video fps",
    ):
        model.generate(
            [VideoInput(video_path, fps=1.0)],
            schema_name="structured_result",
            result_type=StructuredResult,
        )


def test_llama_cpp_wraps_http_errors_with_bounded_server_detail() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"message": "video is not supported in this build"}},
        )

    model = LlamaCppMultimodalModel(
        "llama-cpp-test-model",
        base_url="http://llama-cpp.test",
        max_output_tokens=100,
        request_timeout_seconds=10,
        expected_server_video_fps=0.5,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(
        LlamaCppModelError,
        match="HTTP 503; server detail: video is not supported in this build",
    ):
        model.generate(
            [TextInput("describe")],
            schema_name="structured_result",
            result_type=StructuredResult,
        )


@pytest.mark.parametrize(
    ("completion", "message"),
    [
        ({"choices": []}, "invalid chat completion envelope"),
        (
            {
                "choices": [
                    {
                        "message": {"content": _result().model_dump_json()},
                        "finish_reason": "length",
                    }
                ]
            },
            "exhausted max_output_tokens",
        ),
        (
            {
                "choices": [
                    {
                        "message": {"content": '{"summary":"missing scenes"}'},
                        "finish_reason": "stop",
                    }
                ]
            },
            "does not match schema structured_result",
        ),
    ],
)
def test_llama_cpp_rejects_invalid_structured_responses(
    completion: dict[str, object],
    message: str,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion)

    model = LlamaCppMultimodalModel(
        "llama-cpp-test-model",
        base_url="http://llama-cpp.test",
        max_output_tokens=100,
        request_timeout_seconds=10,
        expected_server_video_fps=0.5,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    with pytest.raises(LlamaCppModelError, match=message):
        model.generate(
            [TextInput("describe")],
            schema_name="structured_result",
            result_type=StructuredResult,
        )
