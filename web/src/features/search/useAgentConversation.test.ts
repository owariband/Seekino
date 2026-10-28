import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiClientError, cancelAgentMessage, createAgentConversation, getAgentConversation, getAgentMessage,
  listAgentMessages, streamAgentMessage, subscribeAgentMessages,
} from "../../api/client";
import type {
  AgentAnswerEvent, AgentConversationView, AgentMessageEventView, AgentMessageSendView,
  AgentMessageView, AgentRenderBlock, VideoChunkSearchHit,
} from "../../api/contracts";
import {
  type AgentProgressEvent,
  useAgentConversation,
} from "./useAgentConversation";

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  cancelAgentMessage: vi.fn(), createAgentConversation: vi.fn(), getAgentConversation: vi.fn(), getAgentMessage: vi.fn(),
  listAgentMessages: vi.fn(), streamAgentMessage: vi.fn(), subscribeAgentMessages: vi.fn(),
}));

const now = Date.parse("2026-09-15T00:00:00Z");
function page<T>(items: T[]) { return { items, total: items.length, hasMore: false, nextCursor: null }; }
function searchResult(seriesIds = ["A", "B"], hits: VideoChunkSearchHit[] = []) { return { seriesIds, searchedVideoIds: ["video"], partial: false, warnings: [], hits }; }
function conversation(conversationId = "conv-A"): AgentConversationView {
  return { conversationId, tenantId: "local-default", title: "Test", messageCount: 0, lastMessagePreview: null, lastMessageAt: null, createdAt: now, updatedAt: now };
}
function message(conversationId: string, overrides: Partial<AgentMessageView> = {}): AgentMessageView {
  return {
    messageId: "assistant", conversationId, seriesIds: ["A"], sequenceNo: 1, role: "ASSISTANT", status: "PROCESSING", phase: "QUEUED", reference: null, content: null,
    modelName: null, searchResult: null,
    renderBlocks: [], attachments: [], timing: null, lastError: null, createdAt: now, updatedAt: now,
    ...overrides, version: overrides.version ?? (overrides.status && overrides.status !== "PROCESSING" ? 2 : 1),
  };
}
function messagePair(assistantMessage: AgentMessageView): AgentMessageSendView {
  return {
    userMessage: message(assistantMessage.conversationId, { messageId: "user", role: "USER", sequenceNo: 0, status: "COMPLETED", content: "query" }),
    assistantMessage,
  };
}
function started(conversationId: string): AgentAnswerEvent {
  return {
    type: "answer.started", occurredAt: now, conversationId, messageId: "assistant", sequence: 1,
    userMessage: message(conversationId, { messageId: "user", sequenceNo: 0, role: "USER", status: "COMPLETED", content: "query" }),
    assistantMessage: message(conversationId),
  };
}
function answerSnapshot(assistantMessage: AgentMessageView, sequence: number): AgentAnswerEvent {
  return {
    type: "answer.snapshot", occurredAt: now, conversationId: assistantMessage.conversationId,
    messageId: assistantMessage.messageId, sequence, ...messagePair(assistantMessage),
  };
}
const snapshots = new Map<string, (snapshot: AgentMessageEventView) => void>();
const close = vi.fn();
const streams = new Map<string, { emit: (event: AgentAnswerEvent) => void; signal: AbortSignal; finish: () => void }>();

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); snapshots.clear(); streams.clear();
  vi.mocked(cancelAgentMessage).mockReset();
  vi.mocked(getAgentMessage).mockReset().mockRejectedValue(new ApiClientError(404, { message: "missing" }));
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
  it("passes a public source tenant without changing the conversation or attachments", async () => {
    const { result } = await ready();
    act(() => { void result.current.send("查找这段", ["shared-series"], null, {}, null, {
      sourceTenantId: "tenant-alice", attachments: [{ mediaUri: "asset://own-attachment" }],
    }); });
    await waitFor(() => expect(streamAgentMessage).toHaveBeenCalledOnce());
    const args = vi.mocked(streamAgentMessage).mock.calls[0]!;
    expect(args[0]).toBe("conv-A");
    expect(args[5]).toEqual(["shared-series"]);
    expect(args[9]).toEqual([{ mediaUri: "asset://own-attachment" }]);
    expect(args[10]).toBe("tenant-alice");
    expect(result.current.conversationId).toBe("conv-A");
  });

  it("confirms acceptance once before the answer finishes and freezes ordered attachment references", async () => {
    const { result } = await ready();
    const onAccepted = vi.fn();
    const attachments = [{ mediaUri: "asset://second" }, { mediaUri: "asset://first" }];
    act(() => { void result.current.send("出处？", [], null, {}, null, { attachments, onAccepted }); });
    await waitFor(() => expect(streams.has("conv-A")).toBe(true));
    expect(onAccepted).not.toHaveBeenCalled();
    attachments[0]!.mediaUri = "asset://changed";
    attachments.reverse();
    expect(vi.mocked(streamAgentMessage).mock.calls[0]![9]).toEqual([{ mediaUri: "asset://second" }, { mediaUri: "asset://first" }]);
    const stream = streams.get("conv-A")!;
    act(() => { stream.emit(started("conv-A")); });
    expect(onAccepted).toHaveBeenCalledOnce();
    expect(result.current.sending).toBe(true);
    act(() => {
      stream.emit({ ...started("conv-A"), sequence: 2 });
      stream.emit({ type: "answer.failed", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, ...messagePair(message("conv-A", { status: "FAILED" })), error: { code: "AGENT_MODEL_ERROR", message: "模型失败" } });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(onAccepted).toHaveBeenCalledOnce();
  });

  it("keeps an unavailable attachment submission unaccepted without retrying as plain text", async () => {
    const { result } = await ready();
    const onAccepted = vi.fn();
    vi.mocked(streamAgentMessage).mockRejectedValueOnce(new ApiClientError(409, { code: "ATTACHMENT_INPUT_UNAVAILABLE", message: "图片问答暂未接入" }));
    vi.mocked(listAgentMessages).mockRejectedValueOnce(new Error("refresh failed"));
    await act(async () => { await result.current.send("出处？", [], null, {}, null, { attachments: [{ mediaUri: "asset://image" }], onAccepted }); });
    expect(onAccepted).not.toHaveBeenCalled();
    expect(getAgentMessage).not.toHaveBeenCalled();
    expect(streamAgentMessage).toHaveBeenCalledOnce();
    expect(vi.mocked(streamAgentMessage).mock.calls[0]![9]).toEqual([{ mediaUri: "asset://image" }]);
    expect(result.current.error).toBe("图片问答暂未接入");
    expect(result.current.messages).toEqual([]);
    expect(result.current.searching).toBe(false);
  });

  it.each(["network error", "clean EOF"])("confirms a persisted submission by idempotency key after %s without a started event", async (ending) => {
    const { result } = await ready();
    const onAccepted = vi.fn();
    const pair = messagePair(message("conv-A", { status: "COMPLETED", content: "回答" }));
    pair.userMessage.attachments = [{ mediaUri: "asset://image", mediaType: null, sizeBytes: null, previewUrl: null }];
    if (ending === "network error") vi.mocked(streamAgentMessage).mockRejectedValueOnce(new TypeError("connection lost"));
    else vi.mocked(streamAgentMessage).mockResolvedValueOnce();
    vi.mocked(getAgentMessage).mockResolvedValueOnce(pair);
    await act(async () => { await result.current.send("出处？", [], null, {}, null, { attachments: [{ mediaUri: "asset://image" }], onAccepted }); });
    expect(getAgentMessage).toHaveBeenCalledWith("conv-A", { idempotencyKey: vi.mocked(streamAgentMessage).mock.calls[0]![2] });
    expect(onAccepted).toHaveBeenCalledOnce();
    expect(result.current.messages[0]?.attachments).toEqual(pair.userMessage.attachments);
    expect(result.current.error).toBeNull();
    expect(streamAgentMessage).toHaveBeenCalledOnce();
  });

  it("keeps drafts unaccepted after an empty stream when the request key cannot be recovered", async () => {
    const { result } = await ready();
    const onAccepted = vi.fn();
    vi.mocked(streamAgentMessage).mockResolvedValueOnce();
    const pair = messagePair(message("conv-A", { status: "COMPLETED", content: "相同正文的历史回答" }));
    vi.mocked(listAgentMessages).mockResolvedValueOnce(page([pair.userMessage, pair.assistantMessage]));
    await act(async () => { await result.current.send("query", [], null, {}, null, { onAccepted }); });
    expect(getAgentMessage).toHaveBeenCalledOnce();
    expect(onAccepted).not.toHaveBeenCalled();
    expect(result.current.error).toBe("未能确认消息是否已接收，草稿已保留");
    expect(result.current.searching).toBe(false);
  });

  it("ignores a late acceptance after switching conversations", async () => {
    const { result, rerender } = await ready("conv-A");
    const onAccepted = vi.fn();
    act(() => { void result.current.send("出处？", [], null, {}, null, { attachments: [{ mediaUri: "asset://image" }], onAccepted }); });
    await waitFor(() => expect(streams.has("conv-A")).toBe(true));
    const stream = streams.get("conv-A")!;
    rerender({ id: "conv-B" });
    await waitFor(() => expect(result.current.conversationId).toBe("conv-B"));
    act(() => { stream.emit(started("conv-A")); stream.finish(); });
    expect(onAccepted).not.toHaveBeenCalled();
    expect(result.current.messages).toEqual([]);
  });

  it("requests backend cancellation and keeps subscriptions until durable termination", async () => {
    const { result } = await ready();
    const stream = await send(result);
    vi.mocked(cancelAgentMessage).mockResolvedValue(messagePair(message("conv-A")));
    await act(async () => { await result.current.stop(); });
    expect(cancelAgentMessage).toHaveBeenCalledWith("conv-A", "assistant");
    expect(stream.signal.aborted).toBe(false);
    expect(close).not.toHaveBeenCalled();
    expect(result.current.cancelling).toBe(true);
    expect(result.current.searching).toBe(true);
    await act(async () => { await result.current.stop(); });
    expect(cancelAgentMessage).toHaveBeenCalledOnce();
    act(() => {
      snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { status: "FAILED", lastError: { code: "AGENT_CANCELLED", message: "已停止生成回答" } })] });
    });
    await waitFor(() => expect(result.current.searching).toBe(false));
    expect(result.current.cancelling).toBe(false);
    expect(result.current.messages[1]?.lastError?.code).toBe("AGENT_CANCELLED");
    expect(stream.signal.aborted).toBe(true);
    act(() => { void result.current.send("second query"); });
    await waitFor(() => expect(streamAgentMessage).toHaveBeenCalledTimes(2));
    act(() => stream.finish());
    expect(result.current.sending).toBe(true);
  });

  it.each(["FAILED", "COMPLETED"] as const)("applies a %s cancellation response and permits the next send without waiting for the old stream", async (status) => {
    const { result } = await ready();
    const stream = await send(result);
    vi.mocked(cancelAgentMessage).mockResolvedValue(messagePair(message("conv-A", { status })));
    await act(async () => { await result.current.stop(); });
    expect(result.current.messages[1]?.status).toBe(status);
    expect(result.current.searching).toBe(false);
    expect(result.current.cancelling).toBe(false);
    expect(stream.signal.aborted).toBe(true);
    expect(close).not.toHaveBeenCalled();
    act(() => { void result.current.send("second query"); });
    await waitFor(() => expect(streamAgentMessage).toHaveBeenCalledTimes(2));
    act(() => stream.finish());
    expect(result.current.sending).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it("shows cancellation errors and allows retrying without detaching the stream", async () => {
    const { result } = await ready();
    const stream = await send(result);
    vi.mocked(cancelAgentMessage).mockRejectedValueOnce(new Error("stop unavailable")).mockResolvedValue(messagePair(message("conv-A")));
    await act(async () => { await result.current.stop(); });
    expect(result.current.error).toBe("stop unavailable");
    expect(result.current.cancelling).toBe(false);
    expect(stream.signal.aborted).toBe(false);
    await act(async () => { await result.current.stop(); });
    expect(cancelAgentMessage).toHaveBeenCalledTimes(2);
    expect(result.current.cancelling).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it("ignores cancellation responses after switching conversations", async () => {
    const { result, rerender } = await ready();
    await send(result);
    let resolve!: (pair: AgentMessageSendView) => void;
    vi.mocked(cancelAgentMessage).mockImplementationOnce(() => new Promise((finish) => { resolve = finish; }));
    act(() => { void result.current.stop(); });
    rerender({ id: "conv-B" });
    await waitFor(() => expect(result.current.conversationId).toBe("conv-B"));
    await act(async () => resolve(messagePair(message("conv-A", { status: "FAILED" }))));
    expect(result.current.messages).toEqual([]);
    expect(result.current.cancelling).toBe(false);
    expect(result.current.error).toBeNull();
  });

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

  it("snapshots message scope and extra context without changing Conversation or interrupting the running stream", async () => {
    const { result, rerender } = await ready();
    const selected = ["A", "B"];
    const reference = { messageId: "previous", videoChunkId: "selected-chunk" };
    const extraContext = {
      playback: { seriesId: "A", videoId: "video-1", episodeOrder: 1, positionSeconds: 12 },
    };
    act(() => { void result.current.send("query", selected, reference, {}, extraContext); });
    await waitFor(() => expect(streams.has("conv-A")).toBe(true));
    const stream = streams.get("conv-A")!;
    selected.splice(0, selected.length, "C");
    reference.videoChunkId = "another-chunk";
    extraContext.playback.positionSeconds = 99;
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
    expect(vi.mocked(streamAgentMessage).mock.calls[0]![6]).toEqual({ messageId: "previous", videoChunkId: "selected-chunk" });
    expect(vi.mocked(streamAgentMessage).mock.calls[0]![8]).toEqual({
      playback: { seriesId: "A", videoId: "video-1", episodeOrder: 1, positionSeconds: 12 },
    });
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
    const hit = { candidate: { videoChunkId: "chunk" }, evidence: { videoId: "video", videoChunkId: "chunk", startSeconds: 12, endSeconds: 15 } } as VideoChunkSearchHit;

    act(() => {
      stream.emit({ type: "answer.block", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 2, block: { type: "video", videoChunkId: "chunk", startSeconds: 12, endSeconds: 14 } });
      stream.emit({ type: "answer.evidence", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, searchResult: searchResult(["A"], [hit]) });
      stream.emit({ type: "answer.block", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 4, block: { type: "markdown", markdown: "片段后的说明" } });
      snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A")] });
    });

    expect(result.current.messages[1]?.renderBlocks).toEqual([
      { type: "video", videoChunkId: "chunk", startSeconds: 12, endSeconds: 14 },
      { type: "markdown", markdown: "片段后的说明" },
    ]);
    expect(result.current.messages[1]?.searchResult?.hits).toEqual([hit]);
  });

  it("keeps inline tools in snapshot order and rejects unrelated, duplicate, and terminal-late snapshots", async () => {
    const { result } = await ready();
    const stream = await send(result);
    const before: AgentRenderBlock = { type: "markdown", markdown: "我先检索。" };
    const tool: AgentRenderBlock = { type: "tool", callId: "call-1", tool: "search_video_graph", status: "inProgress", query: "演出场面" };
    const after: AgentRenderBlock = { type: "markdown", markdown: "已找到相关片段。" };
    const partial = message("conv-A", { version: 2, content: "我先检索。", renderBlocks: [before, tool] });
    act(() => stream.emit(answerSnapshot(partial, 2)));
    expect(result.current.messages[1]?.renderBlocks).toEqual([before, tool]);
    const blocks: AgentRenderBlock[] = [before, { ...tool, status: "completed" }, after];
    const completeTool = message("conv-A", { version: 3, content: "我先检索。已找到相关片段。", renderBlocks: blocks });
    act(() => {
      stream.emit(answerSnapshot(completeTool, 3));
      stream.emit(answerSnapshot({ ...partial, version: 4 }, 3));
      stream.emit(answerSnapshot({ ...partial, conversationId: "conv-B", version: 4 }, 4));
      stream.emit(answerSnapshot({ ...partial, messageId: "other", version: 4 }, 4));
    });
    expect(result.current.messages[1]?.renderBlocks).toEqual(blocks);
    const final = { ...completeTool, status: "COMPLETED" as const, phase: null, version: 4 };
    act(() => {
      stream.emit({ type: "answer.completed", conversationId: "conv-A", messageId: "assistant", sequence: 4, occurredAt: now, ...messagePair(final) });
      stream.emit(answerSnapshot({ ...partial, version: 5 }, 5));
      snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [{ ...partial, version: 5 }] });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(result.current.messages[1]?.status).toBe("COMPLETED");
    expect(result.current.messages[1]?.renderBlocks).toEqual(blocks);
  });

  it("restores persisted inline tools and their positions on remount without replaying old callbacks", async () => {
    const blocks: AgentRenderBlock[] = [
      { type: "markdown", markdown: "先查询网页。" },
      { type: "tool", callId: "call-web", tool: "search_web", status: "completed", query: "乐队演出" },
      { type: "markdown", markdown: "查询后的说明。" },
    ];
    const stored = message("conv-A", { status: "COMPLETED", content: "先查询网页。查询后的说明。", renderBlocks: blocks });
    vi.mocked(listAgentMessages).mockResolvedValue(page([stored]));
    const first = await ready();
    const oldSnapshot = snapshots.get("conv-A")!;
    first.unmount();
    const { result } = await ready();
    expect(result.current.messages[0]?.renderBlocks).toEqual(blocks);
    expect(result.current.messages[0]?.searchResult).toBeNull();
    act(() => oldSnapshot({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", { version: 9, renderBlocks: [] })] }));
    expect(result.current.messages[0]?.renderBlocks).toEqual(blocks);
    expect(streamAgentMessage).not.toHaveBeenCalled();
  });

  it("renders cumulative snapshots in order and replaces revised or withdrawn video cards in place", async () => {
    const { result } = await ready();
    const stream = await send(result);
    const hit = { candidate: { videoChunkId: "chunk" }, evidence: { videoId: "video", videoChunkId: "chunk" } } as VideoChunkSearchHit;
    const before = { type: "markdown" as const, markdown: "片段前的说明" };
    const video = { type: "video" as const, videoChunkId: "chunk", startSeconds: 12, endSeconds: 14 };
    const after = { type: "markdown" as const, markdown: "片段后的说明" };
    act(() => stream.emit(answerSnapshot(message("conv-A", { version: 2, content: before.markdown, renderBlocks: [before] }), 2)));
    expect(result.current.messages[1]?.renderBlocks).toEqual([before]);
    act(() => stream.emit(answerSnapshot(message("conv-A", { version: 3, content: before.markdown, searchResult: searchResult(["A"], [hit]), renderBlocks: [before, video] }), 3)));
    expect(result.current.messages[1]?.renderBlocks).toEqual([before, video]);
    act(() => stream.emit(answerSnapshot(message("conv-A", { version: 4, content: before.markdown + after.markdown, searchResult: searchResult(["A"], [hit]), renderBlocks: [before, video, after] }), 4)));
    expect(result.current.messages[1]?.renderBlocks).toEqual([before, video, after]);
    const revised = { ...video, endSeconds: 15 };
    act(() => stream.emit(answerSnapshot(message("conv-A", { version: 5, content: before.markdown + after.markdown, searchResult: searchResult(["A"], [hit]), renderBlocks: [before, revised, after] }), 5)));
    expect(result.current.messages[1]?.renderBlocks).toEqual([before, revised, after]);
    act(() => stream.emit(answerSnapshot(message("conv-A", { version: 6, content: before.markdown + after.markdown, searchResult: null, renderBlocks: [before, after] }), 6)));
    expect(result.current.messages[1]?.renderBlocks).toEqual([before, after]);
    expect(result.current.messages[1]?.searchResult).toBeNull();
  });

  it("rejects stale snapshots and continues legacy events from the accepted snapshot", async () => {
    const { result } = await ready();
    const stream = await send(result);
    const before = { type: "markdown" as const, markdown: "已保存的正文" };
    const video = { type: "video" as const, videoChunkId: "chunk", startSeconds: 12, endSeconds: 14 };
    const accepted = message("conv-A", { version: 4, content: before.markdown, renderBlocks: [before] });
    act(() => {
      stream.emit(answerSnapshot(accepted, 2));
      stream.emit(answerSnapshot(message("conv-A", { version: 3, content: "旧正文" }), 3));
      stream.emit(answerSnapshot(message("conv-A", { version: 5, content: "重复序号" }), 3));
      stream.emit({ ...answerSnapshot(message("conv-A", { version: 5, content: "错误消息" }), 4), messageId: "other" });
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 4, delta: "后续正文" });
      stream.emit({ type: "answer.block", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 5, block: video });
    });
    expect(result.current.messages[1]?.content).toBe(before.markdown + "后续正文");
    expect(result.current.messages[1]?.renderBlocks).toEqual([before, video]);
    const replacement = message("conv-A", { version: 5, content: "修订正文", renderBlocks: [] });
    act(() => {
      stream.emit(answerSnapshot(replacement, 6));
      stream.emit({ type: "answer.delta", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 7, delta: "继续" });
      stream.emit({ type: "answer.block", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 8, block: video });
    });
    expect(result.current.messages[1]?.content).toBe("修订正文继续");
    expect(result.current.messages[1]?.renderBlocks).toEqual([video]);
  });

  it("does not advance the version of a stream prefix when a newer watch snapshot is held back", async () => {
    const { result } = await ready();
    const stream = await send(result);
    const before = { type: "markdown" as const, markdown: "正文" };
    const video = { type: "video" as const, videoChunkId: "chunk", startSeconds: 12, endSeconds: 14 };
    act(() => stream.emit(answerSnapshot(message("conv-A", {
      version: 2, content: before.markdown, renderBlocks: [before],
    }), 2)));
    act(() => snapshots.get("conv-A")!({
      type: "messages", occurredAt: now, conversationId: "conv-A", messages: [message("conv-A", {
        version: 5, content: "更晚的已保存正文", renderBlocks: [before, video],
      })],
    }));
    expect(result.current.messages[1]?.content).toBe(before.markdown);
    expect(result.current.messages[1]?.version).toBe(2);
    act(() => stream.emit(answerSnapshot(message("conv-A", {
      version: 3, content: before.markdown, renderBlocks: [before, video],
    }), 3)));
    expect(result.current.messages[1]?.version).toBe(3);
    expect(result.current.messages[1]?.renderBlocks).toEqual([before, video]);
  });

  it.each(["COMPLETED", "FAILED"] as const)("preserves snapshot block order in a durable %s response and ignores late processing updates", async (status) => {
    const { result } = await ready();
    const stream = await send(result);
    const blocks = [
      { type: "markdown" as const, markdown: "前文" },
      { type: "video" as const, videoChunkId: "chunk", startSeconds: 12, endSeconds: 14 },
      { type: "markdown" as const, markdown: "后文" },
    ];
    const partial = message("conv-A", { version: 2, content: "前文后文", renderBlocks: blocks });
    const error = { code: "AGENT_MODEL_ERROR", message: "后续请求失败" };
    const final = { ...partial, version: 3, status, phase: null, lastError: status === "FAILED" ? error : null };
    act(() => {
      stream.emit(answerSnapshot(partial, 2));
      const base = { occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, ...messagePair(final) };
      stream.emit(status === "FAILED" ? { ...base, type: "answer.failed", error } : { ...base, type: "answer.completed" });
      stream.emit(answerSnapshot({ ...partial, version: 4, content: "迟到的正文", renderBlocks: [] }, 4));
      snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [{ ...partial, version: 4 }] });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    expect(result.current.messages[1]?.status).toBe(status);
    expect(result.current.messages[1]?.content).toBe("前文后文");
    expect(result.current.messages[1]?.renderBlocks).toEqual(blocks);
  });

  it("adopts newer evidence and ordered blocks from processing snapshots after the stream ends", async () => {
    const { result } = await ready();
    const stream = await send(result);
    const oldHit = { candidate: { videoChunkId: "old" }, evidence: { videoChunkId: "old" } } as VideoChunkSearchHit;
    const newHit = { candidate: { videoChunkId: "new" }, evidence: { videoChunkId: "new" } } as VideoChunkSearchHit;
    const partial = message("conv-A", { version: 2, content: "已保存正文", searchResult: searchResult(["A"], [oldHit]) });
    vi.mocked(listAgentMessages).mockResolvedValue(page([partial]));
    act(() => {
      stream.emit(answerSnapshot(partial, 2));
      stream.emit({ type: "answer.interrupted", occurredAt: now, conversationId: "conv-A", messageId: "assistant", sequence: 3, error: { code: "STREAM_INTERRUPTED", message: "恢复中" } });
      stream.finish();
    });
    await waitFor(() => expect(result.current.sending).toBe(false));
    const recovered = message("conv-A", {
      version: 3, content: "已保存正文及后续", searchResult: searchResult(["A"], [oldHit, newHit]),
      renderBlocks: [{ type: "markdown", markdown: "已保存正文及后续" }, { type: "video", videoChunkId: "new", startSeconds: 12, endSeconds: 14 }],
    });
    act(() => snapshots.get("conv-A")!({ type: "messages", occurredAt: now, conversationId: "conv-A", messages: [recovered] }));
    expect(result.current.messages[1]?.searchResult?.hits).toEqual([oldHit, newHit]);
    expect(result.current.messages[1]?.renderBlocks).toEqual(recovered.renderBlocks);
    expect(result.current.messages[1]?.content).toBe(recovered.content);
  });

  it("migrates the previous selected series' saved Conversation once and restores its messages", async () => {
    localStorage.setItem("seekino-selected-series", "old-series");
    localStorage.setItem("seekino-agent-conversation:local-default:old-series", JSON.stringify({ key: "old-key", id: "conv-legacy" }));
    vi.mocked(listAgentMessages).mockResolvedValue(page([message("conv-legacy", { status: "COMPLETED", content: "原有聊天" })]));
    const { result } = renderHook(() => useAgentConversation());
    await waitFor(() => expect(result.current.conversationId).toBe("conv-legacy"));
    expect(result.current.messages[0]?.content).toBe("原有聊天");
    expect(createAgentConversation).not.toHaveBeenCalled();
    expect(JSON.parse(localStorage.getItem("seekino-agent-conversation:local-default")!)).toEqual({ key: "old-key", id: "conv-legacy" });
  });

  it("uses a new creation key for an unfinished legacy request with the old scope payload", async () => {
    localStorage.setItem("seekino-selected-series", "old-series");
    localStorage.setItem("seekino-agent-conversation:local-default:old-series", JSON.stringify({ key: "old-key" }));
    await ready();
    expect(createAgentConversation).toHaveBeenCalledOnce();
    expect(vi.mocked(createAgentConversation).mock.calls[0]![0]).not.toBe("old-key");
  });

  it("retains genuine deltas through PROCESSING snapshots and rejects duplicate or unrelated events", async () => {
    const { result } = await ready();
    const stream = await send(result);
    const hit = { candidate: { videoChunkId: "chunk" }, evidence: { videoId: "video", videoChunkId: "chunk", startSeconds: 12, endSeconds: 15 } } as VideoChunkSearchHit;
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

  it("persists an explicitly selected Conversation for the next mount", async () => {
    const selected = await ready("conv-B");
    const stored = JSON.parse(localStorage.getItem("seekino-agent-conversation:local-default")!);
    expect(stored).toMatchObject({ id: "conv-B" });
    expect(stored.key).toEqual(expect.any(String));
    selected.unmount();

    const restored = renderHook(() => useAgentConversation());
    await waitFor(() => expect(restored.result.current.conversationId).toBe("conv-B"));
    expect(getAgentConversation).toHaveBeenLastCalledWith("conv-B");
    expect(createAgentConversation).not.toHaveBeenCalled();
  });

  it("does not let a superseded automatic creation overwrite an explicit selection", async () => {
    let finishAutomaticCreation!: (value: AgentConversationView) => void;
    vi.mocked(createAgentConversation).mockImplementationOnce(() => new Promise((resolve) => {
      finishAutomaticCreation = resolve;
    }));
    const rendered = renderHook(({ id }) => useAgentConversation(id), { initialProps: { id: null as string | null } });
    await waitFor(() => expect(createAgentConversation).toHaveBeenCalledOnce());

    rendered.rerender({ id: "conv-B" });
    await waitFor(() => expect(rendered.result.current.conversationId).toBe("conv-B"));
    expect(JSON.parse(localStorage.getItem("seekino-agent-conversation:local-default")!)).toMatchObject({ id: "conv-B" });

    act(() => finishAutomaticCreation(conversation("conv-A")));
    await waitFor(() => expect(createAgentConversation).toHaveBeenCalledOnce());
    expect(JSON.parse(localStorage.getItem("seekino-agent-conversation:local-default")!)).toMatchObject({ id: "conv-B" });
  });

  it("clears a deleted active Conversation and closes its subscription", async () => {
    const { result } = await ready();
    act(() => result.current.clear());
    expect(result.current.conversationId).toBeNull();
    expect(result.current.messages).toEqual([]);
    expect(result.current.loading).toBe(false);
    expect(close).toHaveBeenCalledOnce();
  });

  it("restores a saved Conversation on remount and waits for a running Kick snapshot", async () => {
    localStorage.setItem("seekino-agent-session:local-default", JSON.stringify({ key: "legacy-key", id: "legacy" }));
    const first = await ready(); first.unmount();
    vi.mocked(listAgentMessages).mockResolvedValue(page([message("conv-A")]));
    const { result } = await ready();
    expect(getAgentConversation).toHaveBeenCalledWith("conv-A");
    expect(createAgentConversation).toHaveBeenCalledOnce();
    expect(createAgentConversation).toHaveBeenCalledWith(expect.any(String));
    expect(localStorage.getItem("seekino-agent-session:local-default")).toBeNull();
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
    const savedKey = JSON.parse(localStorage.getItem("seekino-agent-conversation:local-default")!).key;
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
