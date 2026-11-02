from copy import deepcopy

import pytest

from scripts.verify_chat_streaming_trace import verify


def evidence():
    source = {"callId": "call", "deltaSequence": 1, "serverPublishedMonotonicMs": 1200, "beforeModelCompleted": True}
    base = {"requestId": "request", "messageId": "message", "conversationId": "conversation",
            "source": source, "firstProviderTextReceivedMs": 240, "firstTextRenderedMs": 260}
    browser = [{**base, "stage": "answer.snapshot"},
               {**base, "stage": "text.rendered", "renderedChars": 5},
               {**base, "stage": "text.rendered", "renderedChars": 10},
               {**base, "stage": "answer.model.completed", "callId": "call"},
               {**base, "stage": "answer.completed", "status": "completed", "ttMs": 1000}]
    provider = [{"event": "request", "call_id": "call", "payload": {
                    "request": {"stream": True}, "context": {"assistant_message_id": "message", "conversation_id": "conversation"}}},
                {"event": "chat_text_delta", "payload": {"sequence": 1, "received_monotonic": 1.1,
                    "first_text_ms": 100, "before_completion": True, "chars": 5}},
                {"event": "sse", "payload": {"choices": [{"delta": {"content": "Hello"}}]}},
                {"event": "chat_transport_end", "payload": {"terminal": True, "finished_monotonic": 2}}]
    return browser, provider


def test_only_correlated_body_and_visible_growth_certifies_ftt():
    browser, provider = evidence()
    result = verify(browser, provider)
    assert result["valid"] and result["continuous_growth_verified"]
    assert result["ftt_ms"] == 240 and result["tt_ms"] == 1000


@pytest.mark.parametrize("failure", ["nonstream", "buffered_server", "buffered_browser", "missing_delta", "wrong_call", "incomplete", "reasoning_only"])
def test_nonstream_buffered_and_missing_evidence_cannot_produce_ftt(failure):
    browser, provider = deepcopy(evidence())
    if failure == "nonstream":
        provider[0]["payload"]["request"]["stream"] = False
    elif failure == "buffered_server":
        browser[0]["source"]["serverPublishedMonotonicMs"] = 2100
    elif failure == "buffered_browser":
        browser = [browser[0], browser[3], browser[1], browser[2], browser[4]]
    elif failure == "missing_delta":
        provider.pop(1)
    elif failure == "wrong_call":
        provider[0]["call_id"] = "unrelated"
    elif failure == "reasoning_only":
        provider[2]["payload"]["choices"][0]["delta"] = {"reasoning_content": "Hello"}
    else:
        provider[-1]["payload"]["terminal"] = False
    result = verify(browser, provider)
    assert not result["valid"] and result["ftt_ms"] is None
    assert result["reason"]
