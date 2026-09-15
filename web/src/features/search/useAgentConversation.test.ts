import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiClientError, createAgentConversation, createAgentSession, getAgentConversation,
  getAgentSession, listAgentMessages, streamAgentMessage, subscribeAgentMessages,
} from "../../api/client";
import type {
  AgentAnswerEvent, AgentConversationView, AgentMessageEventView,
  AgentMessageView, AgentSessionView, VideoChunkSearchHit,
} from "../../api/contracts";
import { useAgentConversation } from "./useAgentConversation";

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  createAgentConversation: vi.fn(), createAgentSession: vi.fn(),
  getAgentConversation: vi.fn(), getAgentSession: vi.fn(),
  listAgentMessages: vi.fn(), streamAgentMessage: vi.fn(), subscribeAgentMessages: vi.fn(),
}));

const now = "2026-09-15T00:00:00Z";
const session: AgentSessionView = {
  sessionId: "session", workspaceId: "local-default", name: "Test", conversationCount: 0, createdAt: now, updatedAt: now,
};
function conversation(seriesId: string): AgentConversationView {
  return { conversationId: `conv-${seriesId}`, sessionId: "session", workspaceId: "local-default", title: "Test", seriesIds: [seriesId], messageCount: 0, lastMessageAt: null, createdAt: now, updatedAt: now };
}
function message(conversationId: string, overrides: Partial<AgentMessageView> = {}): AgentMessageView {
  return {
    messageId: "assistant", conversationId, sequenceNo: 1, role: "ASSISTANT", status: "PROCESSING", content: null,
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
  vi.mocked(createAgentSession).mockResolvedValue(session);
  vi.mocked(getAgentSession).mockResolvedValue(session);
  vi.mocked(createAgentConversation).mockImplementation(async (_session, seriesId) => conversation(seriesId));
  vi.mocked(getAgentConversation).mockImplementation(async (id) => conversation(id.replace("conv-", "")));
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

async function ready(seriesId = "A") {
  const rendered = renderHook(({ series }) => useAgentConversation(series), { initialProps: { series: seriesId as string | null } });
  await waitFor(() => expect(rendered.result.current.conversationId).toBe(`conv-${seriesId}`));
  return rendered;
}
async function send(result: { current: ReturnType<typeof useAgentConversation> }, id = "conv-A") {
  act(() => { void result.current.send("query"); });
  await waitFor(() => expect(streams.has(id)).toBe(true));
  act(() => { streams.get(id)!.emit(started(id)); });
  return streams.get(id)!;
}

describe("Conversation state", () => {
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
    rerender({ series: "B" });
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
    const first = await ready(); first.unmount();
    vi.mocked(listAgentMessages).mockResolvedValue([message("conv-A")]);
    const { result } = await ready();
    expect(getAgentConversation).toHaveBeenCalledWith("conv-A");
    expect(createAgentConversation).toHaveBeenCalledOnce();
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
    const savedKey = JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default:A")!).key;
    vi.mocked(getAgentConversation).mockRejectedValue(new ApiClientError(404, { message: "deleted" }));
    const second = await ready();
    expect(vi.mocked(createAgentConversation).mock.calls[1]![2]).not.toBe(savedKey);
    second.unmount();
    vi.mocked(getAgentConversation).mockRejectedValue(new TypeError("network down"));
    const { result } = renderHook(() => useAgentConversation("A"));
    await waitFor(() => expect(result.current.error).toBe("network down"));
    expect(createAgentConversation).toHaveBeenCalledTimes(2);
  });
});
