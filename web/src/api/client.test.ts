import { afterEach, describe, expect, it, vi } from "vitest";

import {
  agentMessagesEventsUrl,
  ApiClientError,
  completeUpload,
  createSeries,
  createUploadSession,
  deleteUploadSession,
  createAgentConversation,
  deleteAgentConversation,
  getAgentConversation,
  getAgentGlobalSettings,
  getAgentMessageOptions,
  getJob,
  getParseConfig,
  getParseUsage,
  getSeries,
  getUploadSession,
  imageContentUrl,
  jobEventsUrl,
  listJobs,
  listSeries,
  listAgentConversations,
  listAgentMessages,
  reparseUpload,
  retryUploadParse,
  searchVideos,
  shouldRetryApiRequest,
  sendAgentMessage,
  streamAgentMessage,
  subscribeAgentMessages,
  videoContentUrl,
  updateSeries,
  updateAgentGlobalSettings,
  updateVideoEpisode,
  uploadSeriesImage,
} from "./client";

afterEach(() => vi.unstubAllGlobals());

it("loads the server parse defaults before an upload is configured", async () => {
  const defaults = { version: 1, vision: { fps: 1 } };
  const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify(defaults)));
  vi.stubGlobal("fetch", fetchMock);
  expect(await getParseConfig()).toEqual(defaults);
  expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/uploads/parse-config");
});

describe("Series background routes", () => {
  it("distinguishes omitted fields from explicit clearing and preserves raw JSON text", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await updateSeries({ workspaceId: "local-default", seriesId: "series/id", description: "简介" });
    await updateSeries({ workspaceId: "local-default", seriesId: "series/id", importantExtraData: null });
    const content = ' \n[ { "自定义字段" : true } ]\n ';
    await updateSeries({ workspaceId: "local-default", seriesId: "series/id", importantExtraData: { format: "json", content } });
    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/series/update");
    expect(fetchMock.mock.calls[0]![1]?.method).toBe("POST");
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1]?.body))).toEqual({ workspaceId: "local-default", seriesId: "series/id", description: "简介" });
    expect(JSON.parse(String(fetchMock.mock.calls[1]![1]?.body))).toEqual({ workspaceId: "local-default", seriesId: "series/id", importantExtraData: null });
    expect(JSON.parse(String(fetchMock.mock.calls[2]![1]?.body)).importantExtraData.content).toBe(content);
  });

  it("uploads raw image bytes, not multipart, and uses managed preview URLs", async () => {
    const asset = { imageId: "simg_managed", url: "/api/media/images/content?imageId=simg_managed&workspaceId=local-default", expiresAt: null };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(asset), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File([new Uint8Array([137, 80, 78, 71])], "参考图.png", { type: "image/png" });
    const signal = new AbortController().signal;
    expect(await uploadSeriesImage("series/id", file, signal)).toEqual(asset);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("/api/series/images/upload?workspaceId=local-default&seriesId=series%2Fid");
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("Content-Type")).toBe("image/png");
    expect(init?.body).toBe(file);
    expect(init?.signal).toBe(signal);
    expect(imageContentUrl("image/id", "workspace/id")).toBe("/api/media/images/content?workspaceId=workspace%2Fid&imageId=image%2Fid");
  });

  it("surfaces background validation errors without replacing them with fake success", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: "INVALID_REQUEST", message: "background image does not exist in this Series" }), { status: 400 })));
    await expect(updateSeries({ workspaceId: "local-default", seriesId: "series/id", importantExtraData: { format: "markdown", content: "![参考](asset://invalid)" } })).rejects.toMatchObject({ status: 400, code: "INVALID_REQUEST", message: "background image does not exist in this Series" });
  });
});

describe("Video episode route", () => {
  it("updates only the episode order through the underscore action route", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);

    await updateVideoEpisode({
      workspaceId: "local-default",
      videoId: "video/id",
      episodeOrder: 7,
    });

    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/videos/update_episode");
    expect(fetchMock.mock.calls[0]![1]?.method).toBe("POST");
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1]?.body))).toEqual({
      workspaceId: "local-default",
      videoId: "video/id",
      episodeOrder: 7,
    });
  });
});

describe("parse usage route", () => {
  it("reads usage for the exact task and preserves decimal costs and missing tokens", async () => {
    const payload = { jobId: "job/id", currency: "CNY", totalTokens: null, estimatedCostCny: "0.00000004", items: [] };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const usage = await getParseUsage("job/id");
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/jobs/usage/get?workspaceId=local-default&jobId=job%2Fid");
    expect(usage.estimatedCostCny).toBe("0.00000004");
    expect(usage.totalTokens).toBeNull();
  });
});

describe("API retry contract", () => {
  it("does not retry domain and validation responses", () => {
    for (const status of [400, 404, 409, 422]) {
      expect(
        shouldRetryApiRequest(
          0,
          new ApiClientError(status, {
            code: "REQUEST_REJECTED",
            message: "request rejected",
          }),
        ),
      ).toBe(false);
    }
  });

  it("retries a network or server failure at most once", () => {
    expect(shouldRetryApiRequest(0, new TypeError("network unavailable"))).toBe(true);
    expect(
      shouldRetryApiRequest(
        0,
        new ApiClientError(503, { code: "UNAVAILABLE", message: "unavailable" }),
      ),
    ).toBe(true);
    expect(shouldRetryApiRequest(1, new TypeError("network unavailable"))).toBe(false);
  });
});

describe("Agent Chat routes", () => {
  it("reads and patches global settings with the YAML revision", async () => {
    const payload = { revision: "rev-1", values: { maximum_concurrent_kicks: 4 }, activeValues: { maximum_concurrent_kicks: 4 }, restartRequiredFields: ["maximum_concurrent_kicks"] };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify(payload), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getAgentGlobalSettings()).resolves.toEqual(payload);
    await updateAgentGlobalSettings({ maximum_concurrent_kicks: 6 }, "rev-1");

    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/agent/global-settings");
    expect(String(fetchMock.mock.calls[1]![0])).toBe("/api/agent/global-settings");
    expect(fetchMock.mock.calls[1]![1]?.method).toBe("PATCH");
    expect(JSON.parse(String(fetchMock.mock.calls[1]![1]?.body))).toEqual({
      values: { maximum_concurrent_kicks: 6 },
      revision: "rev-1",
    });
  });

  it("reads server limits and sends a per-message option snapshot", async () => {
    const payload = {
      topK: { defaultValue: 3, minimum: 1, maximum: 20 },
      maxModelCallsPerTurn: { defaultValue: 6, minimum: 1, maximum: 12 },
      maxToolCallsPerTurn: { defaultValue: 24, minimum: 1, maximum: 48 },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(
        String(input).endsWith("/options") ? JSON.stringify(payload) : "{}",
        { headers: { "Content-Type": "application/json" } },
      ));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getAgentMessageOptions()).resolves.toEqual(payload);
    await sendAgentMessage(
      "conversation/id",
      "找摔杯子的画面",
      "message-key",
      ["series/id"],
      null,
      { topK: 5, maxModelCallsPerTurn: 9, maxToolCallsPerTurn: 32 },
      { playback: { seriesId: "series/id", videoId: "video/id", episodeOrder: 1, positionMs: 12_345 } },
    );

    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/agent/messages/options");
    expect(JSON.parse(String(fetchMock.mock.calls[1]![1]?.body))).toEqual({
      workspaceId: "local-default",
      content: "找摔杯子的画面",
      seriesIds: ["series/id"],
      reference: null,
      topK: 5,
      maxModelCallsPerTurn: 9,
      maxToolCallsPerTurn: 32,
      extraContext: {
        playback: { seriesId: "series/id", videoId: "video/id", episodeOrder: 1, positionMs: 12_345 },
      },
    });
  });

  it("keeps identifiers in queries and sends each message's scope", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response("{}", { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await createAgentConversation("conversation-key");
    await listAgentConversations();
    await listAgentConversations(50);
    await getAgentConversation("conversation/id");
    await deleteAgentConversation("conversation/id");
    await listAgentMessages("conversation/id", 499);
    await sendAgentMessage("conversation/id", "找摔杯子的画面", "message-key", ["series/id", "another/id"]);
    await sendAgentMessage("conversation/id", "搜索全部动画", "all-key");
    const calls = fetchMock.mock.calls;
    expect(String(calls[0]![0])).toBe("/api/agent/conversations/create");
    expect(new Headers(calls[0]![1]?.headers).get("Idempotency-Key")).toBe("conversation-key");
    expect(JSON.parse(String(calls[0]![1]?.body))).toEqual({ workspaceId: "local-default" });
    expect(String(calls[1]![0])).toBe("/api/agent/conversations/list?workspaceId=local-default&limit=100");
    expect(String(calls[2]![0])).toBe("/api/agent/conversations/list?workspaceId=local-default&limit=50");
    expect(String(calls[3]![0])).toBe("/api/agent/conversations/get?conversationId=conversation%2Fid&workspaceId=local-default");
    expect(String(calls[4]![0])).toBe("/api/agent/conversations/delete?conversationId=conversation%2Fid&workspaceId=local-default");
    expect(calls[4]![1]?.method).toBe("POST");
    expect(String(calls[5]![0])).toBe("/api/agent/messages/list?conversationId=conversation%2Fid&workspaceId=local-default&afterSequenceNo=499&limit=100");
    expect(String(calls[6]![0])).toBe("/api/agent/messages/send?conversationId=conversation%2Fid");
    expect(new Headers(calls[6]![1]?.headers).get("Accept")).toBe("application/json");
    expect(JSON.parse(String(calls[6]![1]?.body))).toEqual({ workspaceId: "local-default", content: "找摔杯子的画面", seriesIds: ["series/id", "another/id"], reference: null });
    expect(JSON.parse(String(calls[7]![1]?.body)).seriesIds).toEqual([]);
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
    expect(JSON.parse(String(init?.body))).toEqual({
      workspaceId: "local-default",
      content: "query",
      seriesIds: ["A", "B"],
      reference: null,
    });
    controller.close();
    await pending;
    expect(body.locked).toBe(false);
  });

  it("delivers ordered render blocks without resolving video references in the client", async () => {
    const payloads = [
      { sequence: 2, block: { type: "markdown", markdown: "第一段" } },
      { sequence: 3, block: { type: "video", videoChunkId: "chunk-1", startMs: 12_500, endMs: 13_500 } },
    ];
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      for (const payload of payloads) {
        controller.enqueue(encoder.encode(
          `event: answer.block\ndata: ${JSON.stringify({ conversationId: "conversation/id", messageId: "assistant", ...payload })}\n\n`,
        ));
      }
      controller.close();
    } });
    vi.stubGlobal("fetch", vi.fn(async () => response(body)));
    const received = vi.fn();

    await streamAgentMessage("conversation/id", "query", "key", received, new AbortController().signal);

    expect(received.mock.calls.map(([event]) => event.block)).toEqual(payloads.map(({ block }) => block));
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
    await deleteUploadSession("upload/id");
    await retryUploadParse("upload/id", 7);
    await reparseUpload("upload/id", 8);

    const calls = fetchMock.mock.calls;
    expect(String(calls[0]?.[0])).toBe("/api/series/list?workspaceId=local-default&limit=100");
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
    expect(String(calls[9]?.[0])).toBe("/api/uploads/delete?uploadSessionId=upload%2Fid&workspaceId=local-default");
    expect(calls[9]?.[1]?.method).toBe("POST");
    expect(String(calls[10]?.[0])).toBe("/api/uploads/retry-parse?uploadSessionId=upload%2Fid&workspaceId=local-default&expectedJobVersion=7");
    expect(calls[10]?.[1]?.method).toBe("POST");
    expect(String(calls[11]?.[0])).toBe("/api/uploads/reparse?uploadSessionId=upload%2Fid&workspaceId=local-default&expectedJobVersion=8");
    expect(calls[11]?.[1]?.method).toBe("POST");
    expect(jobEventsUrl("job/id")).toBe(
      "/api/jobs/events?jobId=job%2Fid&workspaceId=local-default",
    );
    expect(videoContentUrl("video/id")).toBe(
      "/api/videos/content?videoId=video%2Fid&workspaceId=local-default",
    );
  });
});
