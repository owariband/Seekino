"""Correlate browser evidence with one provider call; never infer FTT from TT.

Usage: python scripts/verify_chat_streaming_trace.py browser.json call/events.jsonl
Browser evidence is the detail of each ``seekino:chat-delivery`` CustomEvent.
Run against one request and its first visible provider call; retain the inputs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def verify(browser: list[dict], provider: list[dict]) -> dict:
    result = {"ftt_ms": None, "valid": False, "reason": "missing live provider body evidence"}
    candidate = next((e for e in browser if e.get("stage") in {"answer.snapshot", "answer.delta"} and e.get("source")), None)
    if candidate is None:
        return result
    source = candidate["source"]
    result.update(call_id=source["callId"], message_id=candidate["messageId"],
                  tt_ms=next((e.get("ttMs") for e in reversed(browser) if e.get("status") == "completed"), None))
    def reject(reason):
        return {**result, "reason": reason}
    request = next((e for e in provider if e.get("event") == "request"), None)
    if request is None or request["payload"].get("request", {}).get("stream") is not True:
        return reject("actual stream=true request is missing")
    context = request["payload"].get("context", {})
    if (request.get("call_id") != source["callId"] or context.get("assistant_message_id") != candidate["messageId"]
            or context.get("conversation_id") != candidate["conversationId"]):
        return reject("provider and browser identities do not match")
    delta = next((e for e in provider if e.get("event") == "chat_text_delta"
                  and e["payload"].get("sequence") == source["deltaSequence"]), None)
    end = next((e for e in provider if e.get("event") == "chat_transport_end"), None)
    if delta is None or end is None or not end["payload"].get("terminal"):
        return reject("missing provider delta or verified completion")
    raw_text = [choice.get("delta", {}).get("content") for event in provider if event.get("event") == "sse"
                for choice in event.get("payload", {}).get("choices", [])
                if isinstance(choice.get("delta", {}).get("content"), str) and choice["delta"]["content"]]
    index = source["deltaSequence"] - 1
    if (index < 0 or index >= len(raw_text) or not any(text.strip() for text in raw_text[:index + 1])
            or delta["payload"].get("chars") != len(raw_text[index])):
        return reject("missing raw provider content delta; reasoning and empty events cannot establish FTT")
    if not source.get("beforeModelCompleted") or not delta["payload"].get("before_completion"):
        return reject("text was first forwarded at model completion")
    if not (delta["payload"]["received_monotonic"] * 1000 <= source["serverPublishedMonotonicMs"]
            < end["payload"]["finished_monotonic"] * 1000):
        return reject("server buffered text until provider completion")
    records = [e for e in browser if e.get("requestId") == candidate["requestId"]
               and e.get("messageId") == candidate["messageId"]]
    completion = next((i for i, e in enumerate(records) if e.get("stage") == "answer.model.completed"
                       and e.get("callId") == source["callId"]), None)
    if completion is None:
        return reject("missing client model-completion boundary")
    rendered = [e for e in records[:completion] if e.get("stage") == "text.rendered" and e.get("renderedChars", 0) > 0]
    if not rendered:
        return reject("no visible browser text before model completion; delivery may be buffered")
    ftt = candidate.get("firstProviderTextReceivedMs")
    if not isinstance(ftt, (int, float)) or ftt < 0:
        return reject("missing client monotonic receipt duration")
    return {**result, "ftt_ms": ftt, "valid": True, "reason": "correlated live provider text and visible browser delivery",
            "first_text_rendered_ms": rendered[0]["firstTextRenderedMs"],
            "continuous_growth_verified": len({e["renderedChars"] for e in rendered}) >= 2,
            "model_first_text_ms": delta["payload"].get("first_text_ms")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("browser", type=Path)
    parser.add_argument("provider", type=Path)
    args = parser.parse_args()
    browser = json.loads(args.browser.read_text())
    provider = [json.loads(line) for line in args.provider.read_text().splitlines() if line.strip()]
    print(json.dumps(verify(browser, provider), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
