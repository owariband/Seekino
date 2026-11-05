import type { AgentAnswerEvent, AgentTextStream } from "./contracts";

// Receive timing is diagnostic evidence, not a substitute for the provider trace
// and browser growth checks required to certify end-to-end streaming FTT.
export interface ChatDeliveryMeasurement {
  conversationId: string;
  requestId: string;
  messageId: string | null;
  startedAtMs: number;
  firstProviderTextReceivedMs: number | null;
  firstTextRenderedMs: number | null;
  fttMs: null;
  fttValidity: "requires_trace" | "unavailable";
  fttReason: string;
  ttMs: number | null;
  terminationMs: number | null;
  status: "processing" | "completed" | "failed" | "interrupted";
  source: AgentTextStream | null;
}

const observations = new Map<string, ReturnType<typeof beginChatDelivery>>();
const key = (conversationId: string, messageId: string) => `${conversationId}:${messageId}`;

export function beginChatDelivery(conversationId: string, requestId: string, now = performance.now()) {
  const measurement: ChatDeliveryMeasurement = {
    conversationId, requestId, messageId: null, startedAtMs: now,
    firstProviderTextReceivedMs: null, firstTextRenderedMs: null, fttMs: null,
    fttValidity: "unavailable", fttReason: "No live provider text evidence received",
    ttMs: null, terminationMs: null, status: "processing", source: null,
  };
  let sequence = 0;
  let lastRenderedChars = 0;
  const publish = (stage: string, detail: Record<string, unknown> = {}) => window.dispatchEvent(new CustomEvent("seekino:chat-delivery", {
    detail: { stage, sequence, atMs: performance.now(), ...structuredClone(measurement), ...detail },
  }));
  const observation = {
    measurement,
    receive(event: AgentAnswerEvent, at = performance.now()) {
      if (measurement.status !== "processing" || event.conversationId !== conversationId
        || !Number.isInteger(event.sequence) || event.sequence <= sequence) return;
      if (event.type === "answer.started") {
        if (measurement.messageId !== null || event.assistantMessage.messageId !== event.messageId) return;
        measurement.messageId = event.messageId;
        observations.set(key(conversationId, event.messageId), observation);
        if (observations.size > 64) observations.delete(observations.keys().next().value!);
      } else if (event.messageId !== measurement.messageId) return;
      sequence = event.sequence;
      // First provider text is carried on the live delta now; a snapshot with a
      // textStream still counts when the transport delivered it as a snapshot.
      const source: AgentTextStream | null | undefined = event.type === "answer.snapshot"
        ? (event.assistantMessage.messageId === event.messageId
           && event.assistantMessage.status === "PROCESSING"
           && Boolean(event.assistantMessage.content?.trim()) ? event.textStream : null)
        : event.type === "answer.delta" ? event.textStream : null;
      if (source && measurement.firstProviderTextReceivedMs === null && source.origin === "provider_delta"
        && source.beforeModelCompleted && source.callId && source.deltaSequence > 0
        && source.modelFirstTextMs !== null && Number.isFinite(source.modelFirstTextMs)) {
        measurement.firstProviderTextReceivedMs = at - now;
        measurement.source = source;
        measurement.fttValidity = "requires_trace";
        measurement.fttReason = "Correlate provider trace and browser growth before certifying FTT";
      }
      if (event.type === "answer.completed" && event.assistantMessage.status === "COMPLETED") {
        measurement.status = "completed";
        measurement.ttMs = at - now;
        measurement.terminationMs = at - now;
      } else if (event.type === "answer.failed" || event.type === "answer.interrupted") {
        measurement.status = event.type === "answer.failed" ? "failed" : "interrupted";
        measurement.terminationMs = at - now;
      }
      publish(event.type, event.type === "answer.model.completed" ? { callId: event.callId } : {});
    },
    rendered(chars: number, at = performance.now()) {
      if (measurement.firstProviderTextReceivedMs === null || chars <= lastRenderedChars) return;
      measurement.firstTextRenderedMs ??= at - now;
      lastRenderedChars = chars;
      publish("text.rendered", { renderedChars: chars });
    },
    interrupt(at = performance.now()) {
      if (measurement.status !== "processing") return;
      measurement.status = "interrupted";
      measurement.terminationMs = at - now;
      publish("transport.interrupted");
    },
  };
  publish("request.started");
  return observation;
}

export function markChatTextRendered(conversationId: string, messageId: string, chars: number) {
  observations.get(key(conversationId, messageId))?.rendered(chars);
}
