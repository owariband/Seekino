import { describe, expect, it } from "vitest";
import { beginChatDelivery } from "./chatDelivery";
import type { AgentAnswerEvent, AgentTextStream } from "./contracts";

const source: AgentTextStream = { origin: "provider_delta", callId: "call-1", deltaSequence: 1,
  modelFirstTextMs: 20, modelElapsedMs: 20, providerReceivedMonotonicMs: 300,
  serverPublishedMonotonicMs: 301, beforeModelCompleted: true };
function event(type: string, sequence: number, content = "", textStream?: AgentTextStream): AgentAnswerEvent {
  return { type, sequence, conversationId: "conv", messageId: "answer", occurredAt: 1,
    assistantMessage: { conversationId: "conv", messageId: "answer", content, status: type === "answer.completed" ? "COMPLETED" : "PROCESSING" },
    userMessage: { conversationId: "conv", messageId: "user" }, textStream,
  } as AgentAnswerEvent;
}

describe("Chat delivery evidence", () => {
  it("separates provider receipt, render observation and durable TT without certifying missing trace", () => {
    const trace = beginChatDelivery("conv", "request", 100);
    trace.receive(event("answer.started", 1), 105);
    trace.receive(event("answer.snapshot", 2, "Hello", source), 125);
    expect(trace.measurement.firstProviderTextReceivedMs).toBe(25);
    expect(trace.measurement.firstTextRenderedMs).toBeNull();
    trace.rendered(5, 140);
    trace.receive(event("answer.completed", 3, "Hello"), 200);
    expect(trace.measurement).toMatchObject({ firstTextRenderedMs: 40, ttMs: 100,
      fttMs: null, fttValidity: "requires_trace", status: "completed" });
  });

  it.each([undefined, { ...source, beforeModelCompleted: false }, { ...source, modelFirstTextMs: null }])(
    "does not turn full responses, terminal flushes or empty provider events into FTT", (provenance) => {
      const trace = beginChatDelivery("conv", "request", 100);
      trace.receive(event("answer.started", 1), 101);
      trace.receive(event("answer.snapshot", 2, "Complete body", provenance), 150);
      trace.receive(event("answer.completed", 3, "Complete body"), 160);
      expect(trace.measurement).toMatchObject({ firstProviderTextReceivedMs: null, fttMs: null, fttValidity: "unavailable", ttMs: 60 });
    },
  );

  it("ignores wrong identities, duplicate or late events and never counts interruption as success TT", () => {
    const trace = beginChatDelivery("conv", "request", 100);
    trace.receive(event("answer.started", 1), 101);
    trace.receive({ ...event("answer.snapshot", 2, "wrong", source), messageId: "other" }, 110);
    trace.receive(event("answer.snapshot", 1, "duplicate", source), 120);
    trace.interrupt(130);
    trace.receive(event("answer.snapshot", 3, "late", source), 140);
    expect(trace.measurement).toMatchObject({ firstProviderTextReceivedMs: null, fttMs: null, ttMs: null, terminationMs: 30 });
  });

  it("records first provider text from a live answer.delta", () => {
    const trace = beginChatDelivery("conv", "request", 100);
    trace.receive(event("answer.started", 1), 105);
    const delta = {
      type: "answer.delta", sequence: 2, conversationId: "conv", messageId: "answer",
      occurredAt: 1, callId: "call-1", newBlock: true, delta: "Hello", textStream: source,
    } as AgentAnswerEvent;
    trace.receive(delta, 125);
    expect(trace.measurement.firstProviderTextReceivedMs).toBe(25);
    expect(trace.measurement.firstTextRenderedMs).toBeNull();
    trace.rendered(5, 140);
    trace.receive(event("answer.completed", 3, "Hello"), 200);
    expect(trace.measurement).toMatchObject({ firstTextRenderedMs: 40, ttMs: 100,
      fttMs: null, fttValidity: "requires_trace", status: "completed" });
  });
});
