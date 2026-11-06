"""Observe model text without a Chat runtime or Server consumer."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
from openai import OpenAI

from gateway.model_events import ModelReasoningDelta, ModelTextDelta, observe_model_reasoning, observe_model_text
from gateway.streaming.chat import request_chat_completion
from gateway.usage import track_model_call


def test_model_text_observers_are_scoped_across_concurrent_requests():
    barrier = Barrier(2)

    def invoke(label):
        observed = []

        class Wire(httpx.SyncByteStream):
            def __iter__(self):
                barrier.wait(timeout=5)
                for text, finish in ((label, None), (None, "stop")):
                    payload = {"id": label, "object": "chat.completion.chunk", "created": 0, "model": "offline",
                               "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]}
                    yield ("data: " + json.dumps(payload) + "\n\n").encode()
                    if text:
                        assert [event.text for event in observed] == [label]
                yield b"data: [DONE]\n\n"

        def respond(request):
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Wire())

        with OpenAI(api_key="offline", base_url="https://offline.invalid/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
            with track_model_call(provider="offline", model="model", operation="generate") as call:
                with observe_model_text(observed.append):
                    response = request_chat_completion(client.chat.completions, stream=True, usage_call=call,
                        model="model", messages=[{"role": "user", "content": label}], timeout=10)
                assert response.choices[0].message.content == label
                assert len(observed) == 1 and isinstance(observed[0], ModelTextDelta)
                assert observed[0].call_id == call.call_id and observed[0].before_completion
        return observed[0].call_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        identities = list(executor.map(invoke, ["first", "second"]))
    assert len(set(identities)) == 2


def test_model_reasoning_deltas_are_emitted_without_changing_content():
    observed = []

    class Wire(httpx.SyncByteStream):
        def __iter__(self):
            for delta, finish in (
                ({"role": "assistant", "reasoning_content": "deliberate "}, None),
                ({"reasoning_content": "carefully"}, None),
                ({"content": "Answer"}, None),
                ({}, "stop"),
            ):
                payload = {"id": "reasoning", "object": "chat.completion.chunk", "created": 0, "model": "offline",
                           "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                yield ("data: " + json.dumps(payload) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

    def respond(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Wire())

    with OpenAI(api_key="offline", base_url="https://offline.invalid/v1", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        with track_model_call(provider="offline", model="model", operation="generate") as call:
            with observe_model_reasoning(observed.append):
                response = request_chat_completion(client.chat.completions, stream=True, usage_call=call,
                    model="model", messages=[{"role": "user", "content": "reason"}], timeout=10)
            assert response.choices[0].message.content == "Answer"
            assert [event.text for event in observed] == ["deliberate ", "carefully"]
            assert all(isinstance(event, ModelReasoningDelta) for event in observed)
            assert all(event.call_id == call.call_id for event in observed)
            assert [event.sequence for event in observed] == [1, 2]
