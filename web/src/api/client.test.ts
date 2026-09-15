import { afterEach, describe, expect, it, vi } from "vitest";

import {
  agentMessagesEventsUrl,
  ApiClientError,
  completeUpload,
  createSeries,
  createUploadSession,
  createAgentConversation,
  getAgentConversation,
  getJob,
  getSeries,
  getUploadSession,
  jobEventsUrl,
  listJobs,
  listSeries,
  listAgentConversations,
  listAgentMessages,
  searchVideos,
  sendAgentMessage,
  streamAgentMessage,
  subscribeAgentMessages,
  videoContentUrl,
} from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("Agent Chat routes", () => {
  it("keeps identifiers in queries and sends each message's scope", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response("{}", { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await createAgentConversation("conversation-key");
    await listAgentConversations();
    await listAgentConversations(50);
    await getAgentConversation("conversation/id");
    await listAgentMessages("conversation/id", 499);
    await sendAgentMessage("conversation/id", "找摔杯子的画面", "message-key", ["series/id", "another/id"]);
    await sendAgentMessage("conversation/id", "搜索全部动画", "all-key");
    const calls = fetchMock.mock.calls;
    expect(String(calls[0]![0])).toBe("/api/agent/conversations/create");
    expect(new Headers(calls[0]![1]?.headers).get("Idempotency-Key")).toBe("conversation-key");
    expect(JSON.parse(String(calls[0]![1]?.body))).toEqual({ workspaceId: "local-default", title: "画面问询" });
    expect(String(calls[1]![0])).toBe("/api/agent/conversations/list?workspaceId=local-default");
    expect(String(calls[2]![0])).toBe("/api/agent/conversations/list?workspaceId=local-default&limit=50");
    expect(String(calls[3]![0])).toBe("/api/agent/conversations/get?conversationId=conversation%2Fid&workspaceId=local-default");
    expect(String(calls[4]![0])).toBe("/api/agent/messages/list?conversationId=conversation%2Fid&workspaceId=local-default&afterSequenceNo=499&limit=500");
    expect(String(calls[5]![0])).toBe("/api/agent/messages/send?conversationId=conversation%2Fid");
    expect(new Headers(calls[5]![1]?.headers).get("Accept")).toBe("application/json");
    expect(JSON.parse(String(calls[5]![1]?.body))).toEqual({ workspaceId: "local-default", content: "找摔杯子的画面", seriesIds: ["series/id", "another/id"], topK: 10 });
    expect(JSON.parse(String(calls[6]![1]?.body)).seriesIds).toEqual([]);
    expect(agentMessagesEventsUrl("conversation/id")).toBe("/api/agent/messages/events?conversationId=conversation%2Fid&workspaceId=local-default");
    expect(videoContentUrl("video/id", "workspace/id")).toBe("/api/videos/content?videoId=video%2Fid&workspaceId=workspace%2Fid");
  });
});

describe("POST answer stream", () => {
  const encoder = new TextEncoder();
  const frame = (delta: string, conversationId = "conversation/id") =>
    `event: answer.delta\ndata: ${JSON.stringify({ conversationId, messageId: "assistant", sequence: 3, delta })}\n\n`;
  const response = (body: ReadableStream<Uint8Array>) =>
    new Response(body, { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });

  it("ignores events without a positive integer sequence", async () => {
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      for (const sequence of [undefined, 0, -1, 1.5, "2"]) {
        controller.enqueue(encoder.encode(`event: answer.delta\ndata: ${JSON.stringify({ conversationId: "conversation/id", messageId: "assistant", sequence, delta: "invalid" })}\n\n`));
      }
      controller.enqueue(encoder.encode(frame("valid")));
      controller.close();
    } });
    vi.stubGlobal("fetch", vi.fn(async () => response(body)));
    const received = vi.fn();
    await streamAgentMessage("conversation/id", "query", "key", received, new AbortController().signal);
    expect(received).toHaveBeenCalledOnce();
    expect(received).toHaveBeenCalledWith(expect.objectContaining({ delta: "valid", sequence: 3 }));
  });

  it("delivers a provider delta before the response finishes", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => response(body));
    vi.stubGlobal("fetch", fetchMock);
    const received = vi.fn();
    let finished = false;
    const pending = streamAgentMessage("conversation/id", "query", "key", received, new AbortController().signal, ["A", "B"]).then(() => { finished = true; });
    controller.enqueue(encoder.encode(frame("第一段")));
    await vi.waitFor(() => expect(received).toHaveBeenCalledWith(expect.objectContaining({ type: "answer.delta", delta: "第一段" })));
    expect(finished).toBe(false);
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit | undefined;
    expect(new Headers(init?.headers).get("Accept")).toBe("text/event-stream");
    expect(JSON.parse(String(init?.body)).seriesIds).toEqual(["A", "B"]);
    controller.close();
    await pending;
    expect(body.locked).toBe(false);
  });

  it("handles split UTF-8, CRLF, multiple data lines, comments, and a final frame", async () => {
    const bytes = encoder.encode(': heartbeat\r\nid: 3\r\nevent: answer.delta\r\ndata: {"conversationId":"conversation/id",\r\ndata: "messageId":"assistant","sequence":2,"delta":"你好"}\r\n\r\n' + frame("foreign", "other") + frame("最后一段").trimEnd());
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    } });
    vi.stubGlobal("fetch", vi.fn(async () => response(body)));
    const received = vi.fn();
    await streamAgentMessage("conversation/id", "query", "key", received, new AbortController().signal);
    expect(received.mock.calls.map(([event]) => event.delta)).toEqual(["你好", "最后一段"]);
  });

  it("cancels an open reader when the conversation subscription is aborted", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ cancel });
    vi.stubGlobal("fetch", vi.fn(async () => response(body)));
    const controller = new AbortController();
    const pending = streamAgentMessage("conversation/id", "query", "key", vi.fn(), controller.signal);
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(body.locked).toBe(true));
    controller.abort();
    await rejected;
    expect(cancel).toHaveBeenCalledOnce();
    expect(body.locked).toBe(false);
  });

  it("releases a malformed stream and reports an HTTP rejection", async () => {
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(encoder.encode("event: answer.delta\ndata: invalid json\n\n"));
    }, cancel });
    vi.stubGlobal("fetch", vi.fn(async () => response(body)));
    await expect(streamAgentMessage("conversation/id", "query", "key", vi.fn(), new AbortController().signal)).rejects.toBeInstanceOf(SyntaxError);
    expect(cancel).toHaveBeenCalledOnce();
    expect(body.locked).toBe(false);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: "MESSAGE_IN_PROGRESS", message: "already running" }), { status: 409 })));
    await expect(streamAgentMessage("conversation/id", "query", "key", vi.fn(), new AbortController().signal)).rejects.toMatchObject({ status: 409, code: "MESSAGE_IN_PROGRESS" });
  });
});

describe("Conversation snapshots", () => {
  it("ignores malformed JSON and other Conversations while keeping the subscription usable", () => {
    let source!: EventTarget;
    const close = vi.fn();
    vi.stubGlobal("EventSource", class extends EventTarget {
      constructor(_url: string) { super(); source = this; }
      close = close;
    });
    const received = vi.fn();
    const windowError = vi.fn();
    window.addEventListener("error", windowError);
    const unsubscribe = subscribeAgentMessages("conversation/id", received);
    try {
      source.dispatchEvent(new MessageEvent("messages", { data: "invalid JSON" }));
      source.dispatchEvent(new MessageEvent("messages", { data: "null" }));
      source.dispatchEvent(new MessageEvent("messages", { data: JSON.stringify({ conversationId: "other", messages: [] }) }));
      source.dispatchEvent(new MessageEvent("messages", { data: JSON.stringify({ conversationId: "conversation/id", messages: [] }) }));
      expect(windowError).not.toHaveBeenCalled();
      expect(received).toHaveBeenCalledOnce();
      expect(received).toHaveBeenCalledWith({ conversationId: "conversation/id", messages: [] });
    } finally {
      unsubscribe();
      window.removeEventListener("error", windowError);
    }
    expect(close).toHaveBeenCalledOnce();
  });
});

describe("current API routes", () => {
  it("uses action routes, query identifiers, headers, and camelCase bodies", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        ({ ok: true, status: 200, json: async () => ({}) }) as Response,
    );
    vi.stubGlobal("fetch", fetchMock);

    await listSeries();
    await getSeries("series/id");
    await createSeries({ workspaceId: "local-default", name: "Series" });
    await createUploadSession("series/id", "upload-key", {
      workspaceId: "local-default",
      episodeOrder: 2,
      originalFilename: "episode-02.mp4",
      mediaType: "video/mp4",
      totalSizeBytes: 1024,
    });
    await getUploadSession("upload/id");
    await completeUpload("upload/id");
    await listJobs();
    await getJob("job/id");
    await searchVideos({
      workspaceId: "local-default",
      query: "rainy doorway",
      seriesIds: ["series/id"],
      topK: 10,
    });

    const calls = fetchMock.mock.calls;
    expect(String(calls[0]?.[0])).toBe("/api/series/list?workspaceId=local-default");
    expect(String(calls[1]?.[0])).toBe(
      "/api/series/get?seriesId=series%2Fid&workspaceId=local-default",
    );
    expect(String(calls[2]?.[0])).toBe("/api/series/create");
    expect(calls[2]?.[1]?.method).toBe("POST");
    expect(String(calls[3]?.[0])).toBe("/api/uploads/create?seriesId=series%2Fid");
    expect(new Headers(calls[3]?.[1]?.headers).get("Idempotency-Key")).toBe(
      "upload-key",
    );
    expect(String(calls[4]?.[0])).toBe(
      "/api/uploads/get?uploadSessionId=upload%2Fid&workspaceId=local-default",
    );
    expect(String(calls[5]?.[0])).toBe(
      "/api/uploads/complete?uploadSessionId=upload%2Fid&workspaceId=local-default",
    );
    expect(String(calls[6]?.[0])).toBe(
      "/api/jobs/list?workspaceId=local-default&limit=100",
    );
    expect(String(calls[7]?.[0])).toBe(
      "/api/jobs/get?jobId=job%2Fid&workspaceId=local-default",
    );
    expect(String(calls[8]?.[0])).toBe("/api/search");
    expect(JSON.parse(String(calls[8]?.[1]?.body))).toEqual({
      workspaceId: "local-default",
      query: "rainy doorway",
      seriesIds: ["series/id"],
      topK: 10,
    });
    expect(jobEventsUrl("job/id")).toBe(
      "/api/jobs/events?jobId=job%2Fid&workspaceId=local-default",
    );
    expect(videoContentUrl("video/id")).toBe(
      "/api/videos/content?videoId=video%2Fid&workspaceId=local-default",
    );
  });
});
