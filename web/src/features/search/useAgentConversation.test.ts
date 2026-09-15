import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiClientError, createAgentConversation, getAgentConversation,
  listAgentMessages, streamAgentMessage, subscribeAgentMessages,
} from "../../api/client";
import type {
  AgentAnswerEvent, AgentConversationView, AgentMessageEventView,
  AgentMessageView, VideoChunkSearchHit,
} from "../../api/contracts";
import { useAgentConversation } from "./useAgentConversation";

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  createAgentConversation: vi.fn(), getAgentConversation: vi.fn(),
  listAgentMessages: vi.fn(), streamAgentMessage: vi.fn(), subscribeAgentMessages: vi.fn(),
}));

const now = "2026-09-15T00:00:00Z";
function conversation(conversationId = "conv-A"): AgentConversationView {
  return { conversationId, workspaceId: "local-default", title: "Test", messageCount: 0, lastMessageAt: null, createdAt: now, updatedAt: now };
}
function message(conversationId: string, overrides: Partial<AgentMessageView> = {}): AgentMessageView {
  return {
    messageId: "assistant", conversationId, seriesIds: ["A"], sequenceNo: 1, role: "ASSISTANT", status: "PROCESSING", content: null,
    modelName: null, replyToMessageId: "user", retrievalQuery: null, searchResult: null, lastError: null, createdAt: now, updatedAt: now, ...overrides,
  };
}
function started(conversationId: string): AgentAnswerEvent {
  return {
    type: "answer.started", conversationId, messageId: "assistant", sequence: 1,
    userMessage: message(conversationId, { messageId: "user", sequenceNo: 0, role: "USER", status: "COMPLETED", content: "query", replyToMessageId: null }),
    assistantMessage: message(conversationId),
  };
}
const snapshots = new Map<string, (snapshot: AgentMessageEventView) => void>();
const close = vi.fn();
const streams = new Map<string, { emit: (event: AgentAnswerEvent) => void; signal: AbortSignal; finish: () => void }>();

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); snapshots.clear(); streams.clear();
  vi.mocked(createAgentConversation).mockImplementation(async () => conversation());
  vi.mocked(getAgentConversation).mockImplementation(async (id) => conversation(id));
  vi.mocked(listAgentMessages).mockResolvedValue([]);
  vi.mocked(subscribeAgentMessages).mockImplementation((id, onSnapshot) => {
    snapshots.set(id, onSnapshot); return close;
  });
  vi.mocked(streamAgentMessage).mockImplementation(async (id, _content, _key, emit, signal) => {
    await new Promise<void>((finish) => streams.set(id, { emit, signal, finish }));
  });
});
afterEach(() => {
  cleanup();
  for (const stream of streams.values()) stream.finish();
});

async function ready(conversationId: string | null = null) {
  const rendered = renderHook(({ id }) => useAgentConversation(id), { initialProps: { id: conversationId } });
  await waitFor(() => expect(rendered.result.current.conversationId).toBe(conversationId ?? "conv-A"));
  return rendered;
}
async function send(result: { current: ReturnType<typeof useAgentConversation> }, id = "conv-A") {
  act(() => { void result.current.send("query"); });
  await waitFor(() => expect(streams.has(id)).toBe(true));
  act(() => { streams.get(id)!.emit(started(id)); });
  return streams.get(id)!;
}

describe("Conversation state", () => {
  it("snapshots a multi-series scope without changing Conversation or interrupting the running stream", async () => {
    const { result, rerender } = await ready();
    const selected = ["A", "B"];
    act(() => { void result.current.send("query", selected); });
    await waitFor(() => expect(streams.has("conv-A")).toBe(true));
    const stream = streams.get("conv-A")!;
    selected.splice(0, selected.length, "C");
    rerender({ id: null });
    act(() => {
      const event = started("conv-A");
      if (event.type === "answer.started") {
        stream.emit({ ...event, userMessage: { ...event.userMessage, seriesIds: ["A", "B"] }, assistantMessage: { ...event.assistantMessage, seriesIds: ["A", "B"] } });
      }
      stream.emit({ type: "answer.evidence", conversationId: "conv-A", messageId: "assistant", sequence: 2, hits: [] });
      stream.emit({ type: "answer.delta", conversationId: "conv-A", messageId: "assistant", sequence: 3, delta: "跨动画回答" });
    });
    expect(vi.mocked(streamAgentMessage).mock.calls[0]![5]).toEqual(["A", "B"]);
    expect(stream.signal.aborted).toBe(false);
    expect(close).not.toHaveBeenCalled();
    expect(createAgentConversation).toHaveBeenCalledOnce();
    expect(result.current.messages[1]?.content).toBe("跨动画回答");
    expect(result.current.messages[1]?.searchResult?.seriesIds).toEqual(["A", "B"]);
  });

  it("sends an empty selection as all and uses the server's resolved scope for streamed evidence", async () => {
    const { result } = await ready();
    act(() => { void result.current.send("query"); });
    await waitFor(() => expect(streams.has("conv-A")).toBe(true));
    const stream = streams.get("conv-A")!;
    act(() => {
      const event = started("conv-A");
      if (event.type === "answer.started") stream.emit({ ...event, assistantMessage: { ...event.assistantMessage, seriesIds: ["A", "B"] } });
      stream.emit({ type: "answer.evidence", conversationId: "conv-A", messageId: "assistant", sequence: 2, hits: [] });
    });
    expect(vi.mocked(streamAgentMessage).mock.calls[0]![5]).toEqual([]);
    expect(result.current.messages[1]?.searchResult?.seriesIds).toEqual(["A", "B"]);
  });

  it("migrates the previous selected series' saved Conversation once and restores its messages", async () => {
    localStorage.setItem("anivag-selected-series", "old-series");
    localStorage.setItem("anivag-agent-conversation:local-default:old-series", JSON.stringify({ key: "old-key", id: "conv-legacy" }));
    vi.mocked(listAgentMessages).mockResolvedValue([message("conv-legacy", { status: "COMPLETED", content: "原有聊天" })]);
    const { result } = renderHook(() => useAgentConversation());
    await waitFor(() => expect(result.current.conversationId).toBe("conv-legacy"));
    expect(result.current.messages[0]?.content).toBe("原有聊天");
    expect(createAgentConversation).not.toHaveBeenCalled();
    expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!)).toEqual({ key: "old-key", id: "conv-legacy" });
  });

  it("uses a new creation key for an unfinished legacy request with the old scope payload", async () => {
    localStorage.setItem("anivag-selected-series", "old-series");
    localStorage.setItem("anivag-agent-conversation:local-default:old-series", JSON.stringify({ key: "old-key" }));
    await ready();
    expect(createAgentConversation).toHaveBeenCalledOnce();
    expect(vi.mocked(createAgentConversation).mock.calls[0]![0]).not.toBe("old-key");
  });

  it("retains genuine deltas through PROCESSING snapshots and rejects duplicate or unrelated events", async () => {
    const { result } = await ready();
    const stream = await send(result);
    const hit = { candidate: { videoChunkId: "chunk" }, evidence: { videoId: "video", videoChunkId: "chunk", startMs: 12000, endMs: 15000 } } as VideoChunkSearchHit;
    act(() => {
      stream.emit({ type: "answer.evidence", conversationId: "conv-A", messageId: "assistant", sequence: 2, hits: [hit] });
      stream.emit({ type: "answer.delta", conversationId: "conv-A", messageId: "assistant", sequence: 3, delta: "第一段" });
      stream.emit({ type: "answer.delta", conversationId: "conv-A", messageId: "assistant", sequence: 3, delta: "重复" });
      stream.emit({ type: "answer.delta", conversationId: "conv-A", messageId: "assistant", delta: "缺序号" } as unknown as AgentAnswerEvent);
      stream.emit({ type: "answer.delta", conversationId: "conv-A", messageId: "wrong", sequence: 4, delta: "其他消息" });
      stream.emit({ type: "answer.delta", conversationId: "conv-B", messageId: "assistant", sequence: 4, delta: "其他会话" });
      snapshots.get("conv-A")!({ conversationId: "conv-A", messages: [message("conv-A")] });
    });
    expect(result.current.messages[1]?.content).toBe("第一段");
    expect(result.current.messages[1]?.searchResult?.hits).toEqual([hit]);
    expect(result.current.searching).toBe(true);
    const final = message("conv-A", { status: "COMPLETED", content: "完整回答", searchResult: { query: "query", seriesIds: ["A"], agentReply: { message: "完整回答", resultCount: 1 }, hits: [hit] } });
    act(() => {
      stream.emit({ ...started("conv-A"), type: "answer.completed", sequence: 4, assistantMessage: final } as AgentAnswerEvent);
      snapshots.get("conv-A")!({ conversationId: "conv-A", messages: [message("conv-A")] });
      stream.emit({ type: "answer.delta", conversationId: "conv-A", messageId: "assistant", sequence: 5, delta: "迟到" });
      stream.finish();
    });
    await waitFor(() => expect(result.current.searching).toBe(false));
    expect(result.current.messages[1]?.content).toBe("完整回答");
    expect(result.current.messages[1]?.status).toBe("COMPLETED");
  });

  it("closes only the old subscriptions on switching and ignores all late callbacks", async () => {
    const { result, rerender } = await ready();
    const oldStream = await send(result);
    const oldSnapshot = snapshots.get("conv-A")!;
    rerender({ id: "conv-B" });
    expect(result.current.messages).toEqual([]);
    await waitFor(() => expect(result.current.conversationId).toBe("conv-B"));
    expect(oldStream.signal.aborted).toBe(true);
    expect(close).toHaveBeenCalledOnce();
    act(() => {
      oldStream.emit({ type: "answer.delta", conversationId: "conv-A", messageId: "assistant", sequence: 2, delta: "迟到回答" });
      oldSnapshot({ conversationId: "conv-A", messages: [message("conv-A", { status: "COMPLETED", content: "旧会话快照" })] });
      oldStream.finish();
    });
    expect(result.current.conversationId).toBe("conv-B");
    expect(result.current.messages).toEqual([]);
  });

  it("restores a saved Conversation on remount and waits for a running Kick snapshot", async () => {
    localStorage.setItem("anivag-agent-session:local-default", JSON.stringify({ key: "legacy-key", id: "legacy" }));
    const first = await ready(); first.unmount();
    vi.mocked(listAgentMessages).mockResolvedValue([message("conv-A")]);
    const { result } = await ready();
    expect(getAgentConversation).toHaveBeenCalledWith("conv-A");
    expect(createAgentConversation).toHaveBeenCalledOnce();
    expect(createAgentConversation).toHaveBeenCalledWith(expect.any(String));
    expect(localStorage.getItem("anivag-agent-session:local-default")).toBeNull();
    expect(result.current.searching).toBe(true);
    act(() => { void result.current.send("second query"); });
    expect(streamAgentMessage).not.toHaveBeenCalled();
    act(() => snapshots.get("conv-A")!({ conversationId: "conv-A", messages: [message("conv-A", { status: "COMPLETED", content: "恢复后的回答" })] }));
    expect(result.current.messages[0]?.content).toBe("恢复后的回答");
    expect(result.current.searching).toBe(false);
  });

  it("keeps PROCESSING after a stream interruption until durable completion arrives", async () => {
    const { result } = await ready();
    const stream = await send(result);
    vi.mocked(listAgentMessages).mockResolvedValue([message("conv-A")]);
    act(() => {
      stream.emit({ type: "answer.delta", conversationId: "conv-A", messageId: "assistant", sequence: 2, delta: "部分回答" });
      stream.emit({ type: "answer.failed", conversationId: "conv-A", messageId: "assistant", sequence: 3, code: "STREAM_INTERRUPTED", message: "订阅中断，正在恢复" });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(result.current.messages[1]?.status).toBe("PROCESSING");
    expect(result.current.messages[1]?.content).toBe("部分回答");
    expect(result.current.searching).toBe(true);
    act(() => snapshots.get("conv-A")!({ conversationId: "conv-A", messages: [message("conv-A", { status: "COMPLETED", content: "最终回答" })] }));
    expect(result.current.messages[1]?.content).toBe("最终回答");
    expect(result.current.searching).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("marks a model failure as failed and never manufactures completed content", async () => {
    const { result } = await ready();
    const stream = await send(result);
    act(() => {
      stream.emit({ type: "answer.failed", conversationId: "conv-A", messageId: "assistant", sequence: 2, code: "AGENT_MODEL_ERROR", message: "模型请求失败", assistantMessage: message("conv-A", { status: "FAILED", lastError: "模型请求失败" }) });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(result.current.messages[1]?.status).toBe("FAILED");
    expect(result.current.messages[1]?.content).toBeNull();
    expect(result.current.error).toBe("模型请求失败");
  });

  it("keeps PROCESSING when a failure could not be persisted and accepts a later durable FAILED snapshot", async () => {
    const { result } = await ready();
    const stream = await send(result);
    vi.mocked(listAgentMessages).mockResolvedValue([message("conv-A")]);
    act(() => {
      stream.emit({ type: "answer.delta", conversationId: "conv-A", messageId: "assistant", sequence: 2, delta: "部分正文" });
      stream.emit({ type: "answer.failed", conversationId: "conv-A", messageId: "assistant", sequence: 3, code: "AGENT_EXECUTION_ERROR", message: "失败状态保存失败" });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(result.current.messages[1]?.status).toBe("PROCESSING");
    expect(result.current.messages[1]?.content).toBe("部分正文");
    expect(result.current.searching).toBe(true);
    expect(result.current.error).toBe("失败状态保存失败");
    act(() => snapshots.get("conv-A")!({ conversationId: "conv-A", messages: [message("conv-A", { status: "FAILED", lastError: "实际失败记录" })] }));
    expect(result.current.messages[1]?.status).toBe("FAILED");
    expect(result.current.messages[1]?.lastError).toBe("实际失败记录");
    expect(result.current.error).toBeNull();
    expect(result.current.searching).toBe(false);
    act(() => snapshots.get("conv-A")!({ conversationId: "conv-A", messages: [message("conv-A")] }));
    expect(result.current.messages[1]?.status).toBe("FAILED");
  });

  it("replaces a deleted Conversation identity and preserves it across network failures", async () => {
    const first = await ready(); first.unmount();
    const savedKey = JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).key;
    vi.mocked(getAgentConversation).mockRejectedValue(new ApiClientError(404, { message: "deleted" }));
    const second = await ready();
    expect(vi.mocked(createAgentConversation).mock.calls[1]![0]).not.toBe(savedKey);
    second.unmount();
    vi.mocked(getAgentConversation).mockRejectedValue(new TypeError("network down"));
    const { result } = renderHook(() => useAgentConversation());
    await waitFor(() => expect(result.current.error).toBe("network down"));
    expect(createAgentConversation).toHaveBeenCalledTimes(2);
  });
});
