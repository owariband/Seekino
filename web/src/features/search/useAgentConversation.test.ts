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
import {
  type AgentProgressEvent,
  useAgentConversation,
} from "./useAgentConversation";

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  createAgentConversation: vi.fn(), getAgentConversation: vi.fn(),
  listAgentMessages: vi.fn(), streamAgentMessage: vi.fn(), subscribeAgentMessages: vi.fn(),
}));

const now = Date.parse("2026-09-15T00:00:00Z");
function page<T>(items: T[]) { return { items, total: items.length, hasMore: false, nextCursor: null }; }
function searchResult(seriesIds = ["A", "B"], hits: VideoChunkSearchHit[] = []) { return { query: "query", seriesIds, searchedVideoIds: ["video"], partial: false, warnings: [], hits }; }
function conversation(conversationId = "conv-A"): AgentConversationView {
  return { conversationId, workspaceId: "local-default", title: "Test", messageCount: 0, lastMessagePreview: null, lastMessageAt: null, createdAt: now, updatedAt: now };
}
function message(conversationId: string, overrides: Partial<AgentMessageView> = {}): AgentMessageView {
  return {
    messageId: "assistant", conversationId, seriesIds: ["A"], sequenceNo: 1, role: "ASSISTANT", status: "PROCESSING", phase: "QUEUED", reference: null, content: null,
    modelName: null, replyToMessageId: "user", retrievalQuery: null, searchResult: null, lastError: null, createdAt: now, updatedAt: now, ...overrides, version: overrides.version ?? (overrides.status && overrides.status !== "PROCESSING" ? 2 : 1),
  };
}
function started(conversationId: string): AgentAnswerEvent {
  return {
    type: "answer.started", occurredAt: now, conversationId, messageId: "assistant", sequence: 1,
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
  vi.mocked(listAgentMessages).mockResolvedValue(page([]));
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
  it("records genuine repeated retrieval/review steps without replaying duplicate or late events", async () => {
    const { result } = await ready();
    const stream = await send(result);
    const phases = ["RETRIEVING", "EVALUATING", "RETRIEVING", "EVALUATING", "RESPONDING"] as const;
    act(() => {
      phases.forEach((phase, index) => stream.emit({
        type: "answer.progress", occurredAt: now + index,
        conversationId: "conv-A", messageId: "assistant",
        sequence: index + 2, phase,
        timing: {
          startedAt: now,
          totalDurationMs: null,
          stages: ["QUEUED", ...phases.slice(0, index)].map((stage) => ({
            phase: stage as AgentProgressEvent["phase"], durationMs: 1,
          })),
        },
      }));
      stream.emit({ type: "answer.progress", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 6, phase: "RESPONDING" });
      stream.emit({ type: "answer.progress", occurredAt: now, conversationId: "conv-B", messageId: "assistant", sequence: 7, phase: "RETRIEVING" });
    });
    expect(result.current.progress.assistant?.map((step) => step.phase)).toEqual(["QUEUED", ...phases]);
    expect(result.current.progress.assistant?.map((step) => step.sequence)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(result.current.messages[1]?.timing?.stages.map((stage) => stage.phase)).toEqual([
      "QUEUED", "RETRIEVING", "EVALUATING", "RETRIEVING", "EVALUATING",
    ]);
    act(() => snapshots.get("conv-A")!({
      type: "messages", occurredAt: now, conversationId: "conv-A",
      messages: [message("conv-A", {
        timing: {
          startedAt: now, totalDurationMs: null,
          stages: [{ phase: "QUEUED", durationMs: 1 }],
        },
      })],
    }));
    expect(result.current.messages[1]?.timing?.stages.map((stage) => stage.phase)).toEqual([
      "QUEUED", "RETRIEVING", "EVALUATING", "RETRIEVING", "EVALUATING",
    ]);
    act(() => {
      stream.emit({
        ...started("conv-A"), type: "answer.completed", sequence: 7,
        assistantMessage: message("conv-A", {
          status: "COMPLETED", phase: null, content: "最终回答",
          timing: {
            startedAt: now, totalDurationMs: 6,
            stages: ["QUEUED", ...phases].map((phase) => ({
              phase: phase as AgentProgressEvent["phase"], durationMs: 1,
            })),
          },
        }),
      } as AgentAnswerEvent);
      stream.emit({ type: "answer.progress", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 8, phase: "RETRIEVING" });
      stream.finish();
    });
    await waitFor(() => expect(result.current.searching).toBe(false));
    expect(result.current.progress.assistant?.map((step) => step.phase)).toEqual(["QUEUED", ...phases]);
    expect(result.current.messages[1]?.timing?.totalDurationMs).toBe(6);
  });

  it("snapshots a multi-series scope without changing Conversation or interrupting the running stream", async () => {
    const { result, rerender } = await ready();
    const selected = ["A", "B"];
    const reference = { messageId: "previous", videoChunkId: "selected-chunk" };
    act(() => { void result.current.send("query", selected, reference); });
    await waitFor(() => expect(streams.has("conv-A")).toBe(true));
    const stream = streams.get("conv-A")!;
    selected.splice(0, selected.length, "C");
    reference.videoChunkId = "another-chunk";
    rerender({ id: null });
    act(() => {
      const event = started("conv-A");
      if (event.type === "answer.started") {
        stream.emit({ ...event, userMessage: { ...event.userMessage, seriesIds: ["A", "B"] }, assistantMessage: { ...event.assistantMessage, seriesIds: ["A", "B"] } });
      }
      stream.emit({ type: "answer.evidence", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, searchResult: searchResult() });
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, delta: "跨动画回答" });
      stream.emit({ type: "answer.progress", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 4, phase: "RESPONDING" });
      snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A")] });
    });
    expect(vi.mocked(streamAgentMessage).mock.calls[0]![5]).toEqual(["A", "B"]);
    expect(vi.mocked(streamAgentMessage).mock.calls[0]![7]).toEqual({ messageId: "previous", videoChunkId: "selected-chunk" });
    expect(stream.signal.aborted).toBe(false);
    expect(close).not.toHaveBeenCalled();
    expect(createAgentConversation).toHaveBeenCalledOnce();
    expect(result.current.messages[1]?.content).toBe("跨动画回答");
    expect(result.current.messages[1]?.phase).toBe("RESPONDING");
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
      stream.emit({ type: "answer.evidence", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, searchResult: searchResult() });
    });
    expect(vi.mocked(streamAgentMessage).mock.calls[0]![5]).toEqual([]);
    expect(result.current.messages[1]?.searchResult?.seriesIds).toEqual(["A", "B"]);
  });

  it("keeps ordered blocks that arrive before evidence and across processing snapshots", async () => {
    const { result } = await ready();
    const stream = await send(result);
    const hit = { candidate: { videoChunkId: "chunk" }, evidence: { videoId: "video", videoChunkId: "chunk", startMs: 12_000, endMs: 15_000 } } as VideoChunkSearchHit;

    act(() => {
      stream.emit({ type: "answer.block", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, block: { type: "video", videoChunkId: "chunk" } });
      stream.emit({ type: "answer.evidence", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, searchResult: searchResult(["A"], [hit]) });
      stream.emit({ type: "answer.block", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 4, block: { type: "markdown", markdown: "片段后的说明" } });
      snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A")] });
    });

    expect(result.current.messages[1]?.renderBlocks).toEqual([
      { type: "video", videoChunkId: "chunk" },
      { type: "markdown", markdown: "片段后的说明" },
    ]);
    expect(result.current.messages[1]?.searchResult?.hits).toEqual([hit]);
  });

  it("migrates the previous selected series' saved Conversation once and restores its messages", async () => {
    localStorage.setItem("anivag-selected-series", "old-series");
    localStorage.setItem("anivag-agent-conversation:local-default:old-series", JSON.stringify({ key: "old-key", id: "conv-legacy" }));
    vi.mocked(listAgentMessages).mockResolvedValue(page([message("conv-legacy", { status: "COMPLETED", content: "原有聊天" })]));
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
      stream.emit({ type: "answer.evidence", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, searchResult: searchResult(["A"], [hit]) });
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, delta: "第一段" });
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, delta: "重复" });
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", delta: "缺序号" } as unknown as AgentAnswerEvent);
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "wrong", sequence: 4, delta: "其他消息" });
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-B", messageId: "assistant", sequence: 4, delta: "其他会话" });
      snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A")] });
    });
    expect(result.current.messages[1]?.content).toBe("第一段");
    expect(result.current.messages[1]?.searchResult?.hits).toEqual([hit]);
    expect(result.current.searching).toBe(true);
    const final = message("conv-A", { status: "COMPLETED", content: "完整回答", searchResult: searchResult(["A"], [hit]) });
    act(() => {
      stream.emit({ ...started("conv-A"), type: "answer.completed", sequence: 4, assistantMessage: final } as AgentAnswerEvent);
      snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A")] });
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 5, delta: "迟到" });
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
    expect(result.current.progress).toEqual({});
    await waitFor(() => expect(result.current.conversationId).toBe("conv-B"));
    expect(oldStream.signal.aborted).toBe(true);
    expect(close).toHaveBeenCalledOnce();
    act(() => {
      oldStream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, delta: "迟到回答" });
      oldSnapshot({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { status: "COMPLETED", content: "旧会话快照" })] });
      oldStream.finish();
    });
    expect(result.current.conversationId).toBe("conv-B");
    expect(result.current.messages).toEqual([]);
  });

  it("restores a saved Conversation on remount and waits for a running Kick snapshot", async () => {
    localStorage.setItem("anivag-agent-session:local-default", JSON.stringify({ key: "legacy-key", id: "legacy" }));
    const first = await ready(); first.unmount();
    vi.mocked(listAgentMessages).mockResolvedValue(page([message("conv-A")]));
    const { result } = await ready();
    expect(getAgentConversation).toHaveBeenCalledWith("conv-A");
    expect(createAgentConversation).toHaveBeenCalledOnce();
    expect(createAgentConversation).toHaveBeenCalledWith(expect.any(String));
    expect(localStorage.getItem("anivag-agent-session:local-default")).toBeNull();
    expect(result.current.searching).toBe(true);
    act(() => { void result.current.send("second query"); });
    expect(streamAgentMessage).not.toHaveBeenCalled();
    act(() => snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { status: "COMPLETED", content: "恢复后的回答" })] }));
    expect(result.current.messages[0]?.content).toBe("恢复后的回答");
    expect(result.current.searching).toBe(false);
  });

  it("keeps PROCESSING after a stream interruption until durable completion arrives", async () => {
    const { result } = await ready();
    const stream = await send(result);
    vi.mocked(listAgentMessages).mockResolvedValue(page([message("conv-A")]));
    act(() => {
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, delta: "部分回答" });
      stream.emit({ type: "answer.interrupted", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, error: { code: "STREAM_INTERRUPTED", message: "订阅中断，正在恢复" } });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(result.current.messages[1]?.status).toBe("PROCESSING");
    expect(result.current.messages[1]?.content).toBe("部分回答");
    expect(result.current.searching).toBe(true);
    act(() => snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { status: "COMPLETED", content: "最终回答" })] }));
    expect(result.current.messages[1]?.content).toBe("最终回答");
    expect(result.current.searching).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("marks a model failure as failed and never manufactures completed content", async () => {
    const { result } = await ready();
    const stream = await send(result);
    act(() => {
      stream.emit({ type: "answer.failed", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, error: { code: "AGENT_MODEL_ERROR", message: "模型请求失败" }, userMessage: message("conv-A", { messageId: "user", role: "USER", status: "COMPLETED", sequenceNo: 0 }), assistantMessage: message("conv-A", { status: "FAILED", lastError: { code: "AGENT_MODEL_ERROR", message: "模型请求失败" } }) });
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
    vi.mocked(listAgentMessages).mockResolvedValue(page([message("conv-A")]));
    act(() => {
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, delta: "部分正文" });
      stream.emit({ type: "answer.interrupted", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, error: { code: "AGENT_EXECUTION_ERROR", message: "失败状态保存失败" } });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(result.current.messages[1]?.status).toBe("PROCESSING");
    expect(result.current.messages[1]?.content).toBe("部分正文");
    expect(result.current.searching).toBe(true);
    expect(result.current.error).toBe("失败状态保存失败");
    act(() => snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { status: "FAILED", lastError: { code: "AGENT_EXECUTION_ERROR", message: "实际失败记录" } })] }));
    expect(result.current.messages[1]?.status).toBe("FAILED");
    expect(result.current.messages[1]?.lastError?.message).toBe("实际失败记录");
    expect(result.current.error).toBeNull();
    expect(result.current.searching).toBe(false);
    act(() => snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A")] }));
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

  it("restores messages beyond the first page using the server cursor and a fixed filter", async () => {
    const messages = Array.from({ length: 101 }, (_, sequenceNo) => message("conv-A", { messageId: `m-${sequenceNo}`, sequenceNo, status: "COMPLETED", content: `message ${sequenceNo}` }));
    vi.mocked(listAgentMessages).mockResolvedValueOnce({ items: messages.slice(0, 100), total: 101, hasMore: true, nextCursor: "opaque-next" }).mockResolvedValueOnce(page(messages.slice(100)));
    const { result } = await ready();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.messages).toHaveLength(101);
    expect(listAgentMessages).toHaveBeenNthCalledWith(1, "conv-A", -1, undefined);
    expect(listAgentMessages).toHaveBeenNthCalledWith(2, "conv-A", -1, "opaque-next");
  });

  it("does not append a checkpoint prefix to the same queued delta or regress phase from processing snapshots", async () => {
    const { result } = await ready();
    const stream = await send(result);
    act(() => {
      stream.emit({ type: "answer.progress", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, phase: "RETRIEVING" });
      snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { version: 3, phase: "EVALUATING", content: "已保存前缀" })] });
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, delta: "已保存前缀" });
    });
    expect(result.current.messages[1]?.content).toBe("已保存前缀");
    expect(result.current.messages[1]?.phase).toBe("RETRIEVING");
    act(() => stream.emit({ type: "answer.progress", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 4, phase: "EVALUATING" }));
    expect(result.current.messages[1]?.phase).toBe("EVALUATING");
    act(() => {
      stream.emit({ type: "answer.interrupted", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 5, error: { code: "STREAM_INTERRUPTED", message: "恢复中" } });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    act(() => snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { version: 4, phase: "RESPONDING", content: "已保存前缀后续正文" })] }));
    expect(result.current.messages[1]?.content).toBe("已保存前缀后续正文");
    expect(result.current.messages[1]?.phase).toBe("RESPONDING");
  });

  it("rejects an older message version even when both terminal updates have the same timestamp", async () => {
    const { result } = await ready();
    act(() => snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { status: "COMPLETED", content: "new result", version: 5 })] }));
    act(() => snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { status: "FAILED", content: "old result", version: 4 })] }));
    expect(result.current.messages[0]?.content).toBe("new result");
    expect(result.current.messages[0]?.version).toBe(5);
  });
});
