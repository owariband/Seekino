import { afterEach, describe, expect, it, vi } from "vitest";

import {
  agentMessagesEventsUrl,
  ApiClientError,
  errorMessage,
  exportParsedVideos,
  completeUpload,
  clearDataSource,
  configureAccountIdentity,
  configureApiIdentity,
  configureDataSource,
  createGuestIdentity,
  createSeries,
  createUploadSession,
  deleteUploadSession,
  createAgentConversation,
  deleteAgentConversation,
  getAgentConversation,
  getAuthIdentity,
  getAgentGlobalSettings,
  getAgentMessageOptions,
  getJob,
  getParseConfig,
  getParseExportOptions,
  getRuntimeConfig,
  getUsageOverview,
  getSeries,
  getUploadSession,
  getVideoParseTask,
  imageContentUrl,
  identityStorageKey,
  jobEventsUrl,
  listJobs,
  listAllVideoParseTasks,
  listVideoParseTasks,
  listSeries,
  listPublicSeries,
  logoutIdentity,
  mediaUrl,
  PRIMARY_CONNECTION,
  listAgentConversations,
  listAgentMessages,
  reparseUpload,
  retryUploadParse,
  searchVideos,
  shouldRetryApiRequest,
  sendAgentMessage,
  sendEmailCode,
  streamAgentMessage,
  subscribeAgentMessages,
  videoContentUrl,
  videoParseTaskEventsUrl,
  updateSeries,
  updateAgentGlobalSettings,
  updateVideoEpisode,
  uploadAgentAttachment,
  uploadSeriesImage,
  uploadSegment,
  verifyEmailCode,
} from "./client";
import type { ApiConnection, AuthIdentity, DataSource, UploadSessionView } from "./contracts";

const localIdentity: AuthIdentity = {
  userId: "local", email: null, isGuest: false, tenantId: "local-default", publicTenantId: "local-default",
  mode: "local", csrfToken: "", permissions: { canUpload: true, canManage: true, canConfigure: true },
};
afterEach(() => {
  clearDataSource();
  configureApiIdentity(localIdentity, "local-default");
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("parsed data export", () => {
  const summary = { record_count: 2, event_count: 7, skipped: { no_matching_events: 1 } };
  const request = { tenantId: "owner-tenant", videoIds: ["video-a"], eventTypes: ["DIALOGUE"] };

  it("loads available types from the requested tenant", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ eventTypes: ["DIALOGUE", "FUTURE_EVENT"] })));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    await expect(getParseExportOptions("owner/tenant", controller.signal)).resolves.toEqual({ eventTypes: ["DIALOGUE", "FUTURE_EVENT"] });
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/videos/parse/export/options?tenantId=owner%2Ftenant");
    controller.abort();
    expect(fetchMock.mock.calls[0]![1]?.signal?.aborted).toBe(true);
  });

  it("downloads a blob with the active credentials and reads only header summary JSON", async () => {
    configureApiIdentity({ ...localIdentity, mode: "cloud", csrfToken: "export-csrf", tenantId: "owner-tenant" }, "owner-tenant");
    const content = '{"text":"爱音：你好"}\n{"text":"灯：你好"}\n';
    const response = new Response(content, { headers: { "Content-Type": "application/x-ndjson", "X-Export-Summary": JSON.stringify(summary) } });
    const readJson = vi.spyOn(response, "json");
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => response);
    vi.stubGlobal("fetch", fetchMock);
    const result = await exportParsedVideos(request);
    expect(await result.blob.text()).toBe(content);
    expect(result.summary).toEqual(summary);
    expect(result.filename).toBe("parse-export.jsonl");
    expect(readJson).not.toHaveBeenCalled();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/videos/parse/export");
    expect(init).toMatchObject({ method: "POST", credentials: "include", body: JSON.stringify(request) });
    expect(new Headers(init?.headers).get("Accept")).toBe("application/x-ndjson");
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("export-csrf");
  });

  it("accepts empty results and omitted type selection", async () => {
    const empty = { record_count: 0, event_count: 0, skipped: { no_matching_events: 3 } };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("", { headers: { "X-Export-Summary": JSON.stringify(empty) } }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await exportParsedVideos({ tenantId: request.tenantId, videoIds: request.videoIds });
    expect(result.blob.size).toBe(0);
    expect(result.summary).toEqual(empty);
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1]?.body))).not.toHaveProperty("eventTypes");
  });

  it.each([403, 404, 409])("preserves HTTP %s export errors", async (status) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: "export_unavailable", message: "来源不可用" }), { status })));
    await expect(exportParsedVideos(request)).rejects.toMatchObject({ status, code: "export_unavailable", message: "来源不可用" });
  });

  it.each([null, "invalid", '{"record_count":-1,"event_count":0,"skipped":{"no_matching_events":0}}'])("rejects missing or invalid summary %s", async (header) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("", { headers: header === null ? {} : { "X-Export-Summary": header } })));
    await expect(exportParsedVideos(request)).rejects.toThrow("导出响应缺少有效统计信息");
  });

  it.each(["caller", "scope"])("rejects a late blob after %s cancellation", async (mode) => {
    const response = new Response("", { headers: { "X-Export-Summary": JSON.stringify(summary) } });
    let release!: (value: Blob) => void;
    const readBlob = vi.spyOn(response, "blob").mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    vi.stubGlobal("fetch", vi.fn(async () => response));
    const controller = new AbortController();
    const pending = exportParsedVideos(request, controller.signal);
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(readBlob).toHaveBeenCalledOnce());
    if (mode === "caller") controller.abort();
    else configureApiIdentity(localIdentity, "another-tenant");
    release(new Blob(["stale data"]));
    await rejected;
  });
});

describe("cloud request identity", () => {
  const guest: AuthIdentity = {
    ...localIdentity, userId: "guest-id", isGuest: true, tenantId: "public-courses", publicTenantId: "public-courses",
    mode: "cloud", csrfToken: "guest-csrf", permissions: { canUpload: false, canManage: false, canConfigure: false },
  };
  const member: AuthIdentity = { ...guest, userId: "member-id", email: "learner@example.com", isGuest: false, tenantId: "personal-id", csrfToken: "member-csrf" };

  it("sends cookies on every auth request and CSRF on writes, without storing session credentials", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(guest)));
    vi.stubGlobal("fetch", fetchMock);
    configureApiIdentity(guest, "public-courses");
    await getAuthIdentity();
    await createGuestIdentity();
    await sendEmailCode("learner@example.com");
    await verifyEmailCode("learner@example.com", "123456");
    await logoutIdentity();
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/auth/me", "/api/auth/guest", "/api/auth/email/send", "/api/auth/email/verify", "/api/auth/logout",
    ]);
    expect(fetchMock.mock.calls.every(([, init]) => init?.credentials === "include")).toBe(true);
    expect(new Headers(fetchMock.mock.calls[0]![1]?.headers).has("X-CSRF-Token")).toBe(false);
    for (const [, init] of fetchMock.mock.calls.slice(1)) expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("guest-csrf");
    expect(JSON.parse(String(fetchMock.mock.calls[3]![1]?.body))).toEqual({ email: "learner@example.com", code: "123456" });
  });

  it("closes the previous user's SSE and rejects late HTTP results before switching the active tenant", async () => {
    const close = vi.fn();
    const sources = vi.fn(function () { return { addEventListener: vi.fn(), close }; });
    vi.stubGlobal("EventSource", sources);
    let release!: (response: Response) => void;
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((resolve) => { release = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    configureApiIdentity(guest, "public-courses");
    subscribeAgentMessages("guest-conversation", () => undefined);
    const request = getSeries("series-id");
    const rejected = expect(request).rejects.toMatchObject({ name: "AbortError" });
    const signal = fetchMock.mock.calls[0]![1]!.signal!;
    configureApiIdentity(member, "personal-id");
    expect(signal.aborted).toBe(true);
    expect(close).toHaveBeenCalledTimes(1);
    expect(sources.mock.calls[0]).toEqual(["/api/agent/messages/events?conversationId=guest-conversation&tenantId=public-courses", { withCredentials: true }]);
    release(new Response(JSON.stringify({ seriesId: "private-result" })));
    await rejected;
    expect(videoContentUrl("video-id")).toBe("/api/videos/content?videoId=video-id&tenantId=personal-id");
    expect(identityStorageKey("anivag-agent-conversation")).toBe(`anivag-agent-conversation:${encodeURIComponent(PRIMARY_CONNECTION.id)}:member-id:personal-id`);
    configureApiIdentity(member, "public-courses");
    expect(identityStorageKey("anivag-agent-conversation")).toBe(`anivag-agent-conversation:${encodeURIComponent(PRIMARY_CONNECTION.id)}:member-id:public-courses`);
  });
});

describe("independent account and data connections", () => {
  const remote: ApiConnection = { id: "remote:https://cloud.example", baseUrl: "/remote" };
  const guest: AuthIdentity = {
    ...localIdentity, mode: "cloud", userId: "guest-id", isGuest: true, tenantId: null,
    publicTenantId: "public-courses", csrfToken: "guest-csrf",
    permissions: { canUpload: false, canManage: false, canConfigure: false },
  };
  const member: AuthIdentity = {
    ...guest, userId: "member-id", email: "learner@example.com", isGuest: false,
    tenantId: "personal-id", csrfToken: "member-csrf",
  };
  const source = (connection = remote, identity = member): DataSource => ({
    key: `${connection.id}:${identity.userId}:personal-id`, kind: "personal",
    connection, tenantId: "personal-id", identity,
  });

  it("reads display mode from the active API without manufacturing a PPE header", async () => {
    configureDataSource(source());
    const query = new AbortController();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ displayMode: "debug" })));
    vi.stubGlobal("fetch", fetchMock);
    await expect(getRuntimeConfig(query.signal)).resolves.toEqual({ displayMode: "debug" });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/remote/api/system/runtime-config");
    expect(init).toMatchObject({ credentials: "include", cache: "no-store" });
    expect(new Headers(init?.headers).has("x-use-ppe")).toBe(false);
    query.abort();
    expect(init?.signal?.aborted).toBe(true);
  });

  it("rejects a late debug config when the data source changes or its query is canceled", async () => {
    configureDataSource(source());
    let release!: (response: Response) => void;
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((resolve) => { release = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    const stale = getRuntimeConfig();
    const rejected = expect(stale).rejects.toMatchObject({ name: "AbortError" });
    configureApiIdentity(localIdentity, "local-default");
    expect(fetchMock.mock.calls[0]![1]?.signal?.aborted).toBe(true);
    release(new Response(JSON.stringify({ displayMode: "debug" })));
    await rejected;

    const query = new AbortController();
    const canceled = getRuntimeConfig(query.signal);
    const canceledResult = expect(canceled).rejects.toMatchObject({ name: "AbortError" });
    query.abort();
    release(new Response(JSON.stringify({ displayMode: "debug" })));
    await canceledResult;
  });

  it("keeps local requests and SSE alive while a remote account logs in or out", async () => {
    configureApiIdentity(localIdentity, "local-default");
    const close = vi.fn();
    vi.stubGlobal("EventSource", vi.fn(function () { return { addEventListener: vi.fn(), close }; }));
    let release!: (response: Response) => void;
    const fetchMock = vi.fn((input: RequestInfo | URL, _init?: RequestInit) =>
      String(input).includes("/series/get")
        ? new Promise<Response>((resolve) => { release = resolve; })
        : Promise.resolve(new Response(JSON.stringify(guest))));
    vi.stubGlobal("fetch", fetchMock);
    subscribeAgentMessages("local-conversation", vi.fn());
    const pending = getSeries("local-series");
    const signal = fetchMock.mock.calls[0]![1]!.signal!;
    configureAccountIdentity(remote, guest);
    await sendEmailCode("learner@example.com", remote);
    configureAccountIdentity(remote, member);
    await logoutIdentity(remote);
    configureAccountIdentity(remote, null);
    expect(signal.aborted).toBe(false);
    expect(close).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.slice(1).map(([url]) => String(url))).toEqual([
      "/remote/api/auth/email/send", "/remote/api/auth/logout",
    ]);
    expect(new Headers(fetchMock.mock.calls[1]![1]?.headers).get("X-CSRF-Token")).toBe("guest-csrf");
    expect(new Headers(fetchMock.mock.calls[2]![1]?.headers).get("X-CSRF-Token")).toBe("member-csrf");
    release(new Response(JSON.stringify({ seriesId: "local-series" })));
    await expect(pending).resolves.toEqual({ seriesId: "local-series" });
    await createSeries({ tenantId: "local-default", name: "Local" });
    expect(new Headers(fetchMock.mock.calls[3]![1]?.headers).has("X-CSRF-Token")).toBe(false);
    expect(identityStorageKey("selection", "legacy-selection")).toBe("legacy-selection");
  });

  it("isolates equal user and tenant IDs on different servers and rejects late local results", async () => {
    configureApiIdentity(localIdentity, "local-default");
    let release!: (response: Response) => void;
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      new Promise<Response>((resolve) => { release = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    const pending = getSeries("local-series");
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    configureAccountIdentity(remote, member);
    configureDataSource(source());
    release(new Response(JSON.stringify({ seriesId: "stale-local-series" })));
    await rejected;
    expect(imageContentUrl("image/id")).toBe("/remote/api/media/images/content?tenantId=personal-id&imageId=image%2Fid");
    expect(videoContentUrl("video/id")).toBe("/remote/api/videos/content?videoId=video%2Fid&tenantId=personal-id");
    expect(jobEventsUrl("job/id")).toBe("/remote/api/jobs/events?jobId=job%2Fid&tenantId=personal-id");
    expect(mediaUrl("/api/videos/content?videoId=video-id")).toBe("/remote/api/videos/content?videoId=video-id");
    expect(mediaUrl("https://cdn.example/video?signature=abc")).toBe("https://cdn.example/video?signature=abc");
    const firstKey = identityStorageKey("selection");
    configureDataSource(source({ id: "remote:https://another.example", baseUrl: "/another" }));
    expect(identityStorageKey("selection")).not.toBe(firstKey);
  });

  it("leaves remote auth available after the data source is revoked", async () => {
    configureDataSource(source());
    clearDataSource();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(guest)));
    vi.stubGlobal("fetch", fetchMock);
    await expect(getSeries("revoked-series")).rejects.toMatchObject({ name: "AbortError" });
    await expect(getAuthIdentity(undefined, remote)).resolves.toEqual(guest);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(String(fetchMock.mock.calls[0]![0])).toBe("/remote/api/auth/me");
  });

  it("keeps caller cancellation and credential refresh tied to the business scope", async () => {
    configureDataSource(source());
    let release!: (response: Response) => void;
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((resolve) => { release = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    const caller = new AbortController();
    const pending = getUploadSession("upload/id", caller.signal);
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    caller.abort();
    expect(fetchMock.mock.calls[0]![1]?.signal?.aborted).toBe(true);
    release(new Response("{}"));
    await rejected;

    const close = vi.fn();
    vi.stubGlobal("EventSource", vi.fn(function () { return { addEventListener: vi.fn(), close }; }));
    subscribeAgentMessages("conversation/id", vi.fn());
    const stale = getSeries("series-id");
    const staleRejected = expect(stale).rejects.toMatchObject({ name: "AbortError" });
    configureDataSource(source(remote, { ...member, csrfToken: "rotated-csrf" }));
    expect(close).toHaveBeenCalledOnce();
    release(new Response("{}"));
    await staleRejected;
  });

  it.each(["canUpload", "canManage", "canConfigure"] as const)("cancels old requests and SSE when %s is withdrawn without changing credentials", async (permission) => {
    const granted = { ...member, permissions: { canUpload: true, canManage: true, canConfigure: true } };
    configureDataSource(source(remote, granted));
    const close = vi.fn();
    vi.stubGlobal("EventSource", vi.fn(function () { return { addEventListener: vi.fn(), close }; }));
    subscribeAgentMessages("conversation/id", vi.fn());
    let release!: (payload: unknown) => void;
    const readPayload = vi.fn(() => new Promise((resolve) => { release = resolve; }));
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({ ok: false, status: 403, json: readPayload } as unknown as Response));
    vi.stubGlobal("fetch", fetchMock);
    const pending = getSeries("series-id");
    const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.waitFor(() => expect(readPayload).toHaveBeenCalledOnce());
    configureDataSource(source(remote, { ...granted, permissions: { ...granted.permissions, [permission]: false } }));
    expect(fetchMock.mock.calls[0]![1]?.signal?.aborted).toBe(true);
    expect(close).toHaveBeenCalledOnce();
    release({ message: "late old scope error" });
    await rejected;
  });

  it("identifies the connection behind a business authentication failure", async () => {
    configureDataSource(source());
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    const required = vi.fn();
    window.addEventListener("anivag-auth-required", required);
    try {
      await expect(getSeries("series-id")).rejects.toMatchObject({ status: 401 });
      expect(required).toHaveBeenCalledWith(expect.objectContaining({ detail: { connectionId: remote.id } }));
    } finally {
      window.removeEventListener("anivag-auth-required", required);
    }
  });

  it("routes segmented uploads and answer streams with the selected source's credentials", async () => {
    configureAccountIdentity(remote, member);
    configureDataSource(source());
    const open = vi.fn();
    const headers = vi.fn();
    vi.stubGlobal("crypto", { subtle: { digest: vi.fn(async () => new Uint8Array([1, 2]).buffer) } });
    vi.stubGlobal("XMLHttpRequest", class {
      upload = { onprogress: null };
      status = 200;
      withCredentials = false;
      onload: (() => void) | null = null;
      open = open;
      setRequestHeader = headers;
      send() { this.onload?.(); }
      abort() {}
    });
    const body = { size: 4, arrayBuffer: async () => new Uint8Array([1, 2, 3, 4]).buffer };
    const file = { size: 4, slice: () => body } as unknown as File;
    const session = { uploadSessionId: "upload/id", preferredSegmentSizeBytes: 4 } as UploadSessionView;
    await expect(uploadSegment({ file, session, segmentNumber: 0, signal: new AbortController().signal, onProgress: vi.fn() })).resolves.toBe(4);
    expect(open).toHaveBeenCalledWith("PUT", "/remote/api/uploads/segments/upload?uploadSessionId=upload%2Fid&segmentNumber=0&tenantId=personal-id");
    expect(headers).toHaveBeenCalledWith("X-CSRF-Token", "member-csrf");
    expect(headers).toHaveBeenCalledWith("Content-Range", "bytes 0-3/4");
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(new ReadableStream({ start(controller) { controller.close(); } }), { headers: { "Content-Type": "text/event-stream" } }));
    vi.stubGlobal("fetch", fetchMock);
    await streamAgentMessage("conversation/id", "query", "key", vi.fn(), new AbortController().signal);
    expect(String(fetchMock.mock.calls[0]![0])).toBe("/remote/api/agent/messages/send?conversationId=conversation%2Fid");
    expect(new Headers(fetchMock.mock.calls[0]![1]?.headers).get("X-CSRF-Token")).toBe("member-csrf");
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1]?.body)).tenantId).toBe("personal-id");
  });

  it("matches only the configured upstream API origin when rewriting proxied media", async () => {
    vi.stubEnv("VITE_REMOTE_API_BASE_URL", "/remote");
    vi.stubEnv("VITE_REMOTE_API_ORIGIN", "https://cloud.example");
    vi.resetModules();
    const client = await import("./client");
    expect(client.REMOTE_CONNECTION).toEqual({ id: "remote:https://cloud.example", baseUrl: "/remote" });
    client.configureDataSource(source(client.REMOTE_CONNECTION!));
    expect(client.mediaUrl("https://cloud.example/api/videos/content?videoId=1")).toBe("/remote/api/videos/content?videoId=1");
    expect(client.mediaUrl("https://cloud.example/files/video.mp4?signature=1")).toBe("https://cloud.example/files/video.mp4?signature=1");
    expect(client.mediaUrl("https://cdn.example/api/video?signature=1")).toBe("https://cdn.example/api/video?signature=1");
    expect(client.mediaUrl("//cloud.example/api/video")).toBeNull();
    expect(client.mediaUrl("http://")).toBeNull();
    client.clearDataSource();
    vi.resetModules();
  });
});

it("loads the server parse defaults before an upload is configured", async () => {
  const defaults = { version: 1, vision: { fps: 1 } };
  const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify(defaults)));
  vi.stubGlobal("fetch", fetchMock);
  expect(await getParseConfig()).toEqual(defaults);
  expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/uploads/parse-config");
});

describe("Chat attachments", () => {
  it("keeps conversation ownership separate from public search scope in JSON and SSE", async () => {
    configureApiIdentity({ ...localIdentity, tenantId: "viewer-tenant" }, "viewer-tenant");
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => new Headers(init?.headers).get("Accept") === "text/event-stream"
      ? new Response("", { headers: { "Content-Type": "text/event-stream" } }) : new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await sendAgentMessage("conversation", "query", "key", ["public-series"], null, {}, null, [], "owner-tenant");
    await streamAgentMessage("conversation", "query", "key", vi.fn(), new AbortController().signal, ["public-series"], null, {}, null, [], "owner-tenant");
    for (const [, init] of fetchMock.mock.calls) {
      expect(JSON.parse(String(init?.body))).toMatchObject({ tenantId: "viewer-tenant", sourceTenantId: "owner-tenant", seriesIds: ["public-series"] });
    }
  });

  it("uploads raw bytes under the active conversation with cookies, CSRF and cancellation", async () => {
    configureApiIdentity({ ...localIdentity, csrfToken: "chat-csrf" }, "tenant/id");
    const attachment = { mediaUri: "asset://attachment", mediaType: "image/png", sizeBytes: 4, previewUrl: "/api/agent/attachments/content?mediaUri=asset" };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(attachment), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File([new Uint8Array([137, 80, 78, 71])], "参考图.png", { type: "image/png" });
    const controller = new AbortController();
    expect(await uploadAgentAttachment("conversation/id", file, controller.signal)).toEqual(attachment);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("/api/agent/attachments/upload?tenantId=tenant%2Fid&conversationId=conversation%2Fid");
    expect(init?.method).toBe("POST");
    expect(init?.credentials).toBe("include");
    expect(new Headers(init?.headers).get("X-CSRF-Token")).toBe("chat-csrf");
    expect(new Headers(init?.headers).get("Content-Type")).toBe("image/png");
    expect(init?.body).toBe(file);
    controller.abort();
    expect(init?.signal?.aborted).toBe(true);
  });

  it("discards an upload response after changing the active identity", async () => {
    let resolve!: (response: Response) => void;
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => new Promise<Response>((finish) => { resolve = finish; }));
    vi.stubGlobal("fetch", fetchMock);
    const upload = uploadAgentAttachment("conversation/id", new File(["data"], "image.png"));
    configureApiIdentity({ ...localIdentity, userId: "other-user" }, "other-tenant");
    resolve(new Response(JSON.stringify({ mediaUri: "asset://old-scope" })));
    await expect(upload).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock.mock.calls[0]![1]?.signal?.aborted).toBe(true);
  });

  it("sends identical ordered attachment references in JSON and SSE, without preview metadata", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => new Headers(init?.headers).get("Accept") === "text/event-stream"
      ? new Response("", { headers: { "Content-Type": "text/event-stream" } })
      : new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    const attachments = [
      { mediaUri: "asset://second", previewUrl: "/preview-2" },
      { mediaUri: "asset://first", previewUrl: "/preview-1" },
    ];
    const context = { playback: { positionSeconds: 1000 } };
    await sendAgentMessage("conversation/id", "出处？", "key", ["series"], null, { topK: 3 }, context, attachments);
    await streamAgentMessage("conversation/id", "出处？", "key", vi.fn(), new AbortController().signal, ["series"], null, { topK: 3 }, context, attachments);
    const jsonBody = JSON.parse(String(fetchMock.mock.calls[0]![1]?.body));
    const streamBody = JSON.parse(String(fetchMock.mock.calls[1]![1]?.body));
    expect(streamBody).toEqual(jsonBody);
    expect(jsonBody.attachments).toEqual([{ mediaUri: "asset://second" }, { mediaUri: "asset://first" }]);
    expect(jsonBody.extraContext).toEqual(context);
  });

  it("preserves attachment validation and unavailable errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: "ATTACHMENT_INPUT_UNAVAILABLE", message: "图片问答暂未接入" }), { status: 409 })));
    await expect(streamAgentMessage("conversation/id", "出处？", "key", vi.fn(), new AbortController().signal, [], null, {}, null, [{ mediaUri: "asset://image" }]))
      .rejects.toMatchObject({ status: 409, code: "ATTACHMENT_INPUT_UNAVAILABLE", message: "图片问答暂未接入" });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: "INVALID_REQUEST", message: "只支持静态图片" }), { status: 400 })));
    await expect(uploadAgentAttachment("conversation/id", new File(["GIF89a"], "animated.gif", { type: "image/gif" })))
      .rejects.toMatchObject({ status: 400, message: "只支持静态图片" });
  });
});

describe("Series background routes", () => {
  it("keeps the viewer identity while addressing a public series by its owning tenant", async () => {
    const identity = { ...localIdentity, mode: "cloud" as const, userId: "viewer", csrfToken: "viewer-csrf", tenantId: "viewer-tenant" };
    configureApiIdentity(identity, "viewer-tenant");
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await listPublicSeries("next/page", 20);
    await getSeries("series/id", "owner/tenant");
    await updateSeries({ tenantId: "owner/tenant", seriesId: "series/id", visibility: "public" });
    await uploadSeriesImage("series/id", new File(["image"], "cover.png"), undefined, "owner/tenant");
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/series/public?limit=20&cursor=next%2Fpage",
      "/api/series/get?seriesId=series%2Fid&tenantId=owner%2Ftenant",
      "/api/series/update",
      "/api/series/images/upload?tenantId=owner%2Ftenant&seriesId=series%2Fid",
    ]);
    expect(JSON.parse(String(fetchMock.mock.calls[2]![1]?.body))).toEqual({ tenantId: "owner/tenant", seriesId: "series/id", visibility: "public" });
    expect(new Headers(fetchMock.mock.calls[2]![1]?.headers).get("X-CSRF-Token")).toBe("viewer-csrf");
    expect(videoContentUrl("video-id")).toContain("tenantId=viewer-tenant");
  });

  it("distinguishes omitted fields from explicit clearing and preserves raw JSON text", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await updateSeries({ tenantId: "local-default", seriesId: "series/id", description: "简介" });
    await updateSeries({ tenantId: "local-default", seriesId: "series/id", importantExtraData: null });
    const content = ' \n[ { "自定义字段" : true } ]\n ';
    await updateSeries({ tenantId: "local-default", seriesId: "series/id", importantExtraData: { format: "json", content } });
    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/series/update");
    expect(fetchMock.mock.calls[0]![1]?.method).toBe("POST");
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1]?.body))).toEqual({ tenantId: "local-default", seriesId: "series/id", description: "简介" });
    expect(JSON.parse(String(fetchMock.mock.calls[1]![1]?.body))).toEqual({ tenantId: "local-default", seriesId: "series/id", importantExtraData: null });
    expect(JSON.parse(String(fetchMock.mock.calls[2]![1]?.body)).importantExtraData.content).toBe(content);
  });

  it("uploads raw image bytes, not multipart, and uses managed preview URLs", async () => {
    const asset = { imageId: "simg_managed", url: "/api/media/images/content?imageId=simg_managed&tenantId=local-default", expiresAt: null };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(asset), { status: 201 }));
    vi.stubGlobal("fetch", fetchMock);
    const file = new File([new Uint8Array([137, 80, 78, 71])], "参考图.png", { type: "image/png" });
    const signal = new AbortController().signal;
    expect(await uploadSeriesImage("series/id", file, signal)).toEqual(asset);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe("/api/series/images/upload?tenantId=local-default&seriesId=series%2Fid");
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("Content-Type")).toBe("image/png");
    expect(init?.body).toBe(file);
    expect(init?.signal?.aborted).toBe(false);
    expect(imageContentUrl("image/id", "workspace/id")).toBe("/api/media/images/content?tenantId=workspace%2Fid&imageId=image%2Fid");
  });

  it("surfaces background validation errors without replacing them with fake success", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ code: "INVALID_REQUEST", message: "background image does not exist in this Series" }), { status: 400 })));
    await expect(updateSeries({ tenantId: "local-default", seriesId: "series/id", importantExtraData: { format: "markdown", content: "![参考](asset://invalid)" } })).rejects.toMatchObject({ status: 400, code: "INVALID_REQUEST", message: "background image does not exist in this Series" });
  });
});

describe("Video episode route", () => {
  it("updates only the episode order through the underscore action route", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);

    await updateVideoEpisode({
      tenantId: "local-default",
      videoId: "video/id",
      episodeOrder: 7,
    });

    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/videos/update_episode");
    expect(fetchMock.mock.calls[0]![1]?.method).toBe("POST");
    expect(JSON.parse(String(fetchMock.mock.calls[0]![1]?.body))).toEqual({
      tenantId: "local-default",
      videoId: "video/id",
      episodeOrder: 7,
    });
  });
});

describe("local Token usage overview route", () => {
  it("requests one overview with encoded filters and preserves known subtotals and missing usage", async () => {
    const payload = { source: "local", status: "available", totals: { totalTokens: null, knownTokens: 42 }, models: [], runs: [] };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify(payload)));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    const usage = await getUsageOverview({ fromStartTime: 1, toStartTime: 2, seriesId: "series/id", runId: "run/id" }, signal);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/usage/overview?tenantId=local-default&fromStartTime=1&toStartTime=2&seriesId=series%2Fid&runId=run%2Fid");
    expect(fetchMock.mock.calls[0]?.[1]?.signal?.aborted).toBe(false);
    expect(usage.source).toBe("local");
    expect(usage.totals.knownTokens).toBe(42);
    expect(usage.totals.totalTokens).toBeNull();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("leaves the default time window to the server and omits empty filters", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    await getUsageOverview({ seriesId: "", runId: undefined });
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/usage/overview?tenantId=local-default");
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
      { playback: { seriesId: "series/id", videoId: "video/id", episodeOrder: 1, positionSeconds: 12 } },
    );

    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/agent/messages/options");
    expect(JSON.parse(String(fetchMock.mock.calls[1]![1]?.body))).toEqual({
      tenantId: "local-default",
      content: "找摔杯子的画面",
      seriesIds: ["series/id"],
      reference: null,
      topK: 5,
      maxModelCallsPerTurn: 9,
      maxToolCallsPerTurn: 32,
      extraContext: {
        playback: { seriesId: "series/id", videoId: "video/id", episodeOrder: 1, positionSeconds: 12 },
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
    expect(JSON.parse(String(calls[0]![1]?.body))).toEqual({ tenantId: "local-default" });
    expect(String(calls[1]![0])).toBe("/api/agent/conversations/list?tenantId=local-default&limit=100");
    expect(String(calls[2]![0])).toBe("/api/agent/conversations/list?tenantId=local-default&limit=50");
    expect(String(calls[3]![0])).toBe("/api/agent/conversations/get?conversationId=conversation%2Fid&tenantId=local-default");
    expect(String(calls[4]![0])).toBe("/api/agent/conversations/delete?conversationId=conversation%2Fid&tenantId=local-default");
    expect(calls[4]![1]?.method).toBe("POST");
    expect(String(calls[5]![0])).toBe("/api/agent/messages/list?conversationId=conversation%2Fid&tenantId=local-default&afterSequenceNo=499&limit=100");
    expect(String(calls[6]![0])).toBe("/api/agent/messages/send?conversationId=conversation%2Fid");
    expect(new Headers(calls[6]![1]?.headers).get("Accept")).toBe("application/json");
    expect(JSON.parse(String(calls[6]![1]?.body))).toEqual({ tenantId: "local-default", content: "找摔杯子的画面", seriesIds: ["series/id", "another/id"], reference: null });
    expect(JSON.parse(String(calls[7]![1]?.body)).seriesIds).toEqual([]);
    expect(agentMessagesEventsUrl("conversation/id")).toBe("/api/agent/messages/events?conversationId=conversation%2Fid&tenantId=local-default");
    expect(videoContentUrl("video/id", "workspace/id")).toBe("/api/videos/content?videoId=video%2Fid&tenantId=workspace%2Fid");
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
      tenantId: "local-default",
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
      { sequence: 3, block: { type: "video", videoChunkId: "chunk-1", startSeconds: 12, endSeconds: 13 } },
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

  it("stops buffered answer events immediately when the first event changes data source", async () => {
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(encoder.encode(frame("first") + frame("stale")));
      controller.close();
    } });
    vi.stubGlobal("fetch", vi.fn(async () => response(body)));
    const received = vi.fn(() => configureApiIdentity(localIdentity, "another-tenant"));
    await expect(streamAgentMessage("conversation/id", "query", "key", received, new AbortController().signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(received).toHaveBeenCalledOnce();
    expect(received).toHaveBeenCalledWith(expect.objectContaining({ delta: "first" }));
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
    await createSeries({ tenantId: "local-default", name: "Series" });
    await createUploadSession("series/id", "upload-key", {
      tenantId: "local-default",
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
      tenantId: "local-default",
      query: "rainy doorway",
      seriesIds: ["series/id"],
      topK: 10,
    });
    await deleteUploadSession("upload/id");
    await retryUploadParse("upload/id", 7, 9);
    await reparseUpload("upload/id", 8);

    const calls = fetchMock.mock.calls;
    expect(String(calls[0]?.[0])).toBe("/api/series/list?tenantId=local-default&limit=100");
    expect(String(calls[1]?.[0])).toBe(
      "/api/series/get?seriesId=series%2Fid&tenantId=local-default",
    );
    expect(String(calls[2]?.[0])).toBe("/api/series/create");
    expect(calls[2]?.[1]?.method).toBe("POST");
    expect(String(calls[3]?.[0])).toBe("/api/uploads/create?seriesId=series%2Fid");
    expect(new Headers(calls[3]?.[1]?.headers).get("Idempotency-Key")).toBe(
      "upload-key",
    );
    expect(String(calls[4]?.[0])).toBe(
      "/api/uploads/get?uploadSessionId=upload%2Fid&tenantId=local-default",
    );
    expect(String(calls[5]?.[0])).toBe(
      "/api/uploads/complete?uploadSessionId=upload%2Fid&tenantId=local-default",
    );
    expect(String(calls[6]?.[0])).toBe(
      "/api/jobs/list?tenantId=local-default&limit=100",
    );
    expect(String(calls[7]?.[0])).toBe(
      "/api/jobs/get?jobId=job%2Fid&tenantId=local-default",
    );
    expect(String(calls[8]?.[0])).toBe("/api/search");
    expect(JSON.parse(String(calls[8]?.[1]?.body))).toEqual({
      tenantId: "local-default",
      query: "rainy doorway",
      seriesIds: ["series/id"],
      topK: 10,
    });
    expect(String(calls[9]?.[0])).toBe("/api/uploads/delete?uploadSessionId=upload%2Fid&tenantId=local-default");
    expect(calls[9]?.[1]?.method).toBe("POST");
    expect(String(calls[10]?.[0])).toBe("/api/uploads/retry-parse?uploadSessionId=upload%2Fid&tenantId=local-default&expectedJobVersion=7&expectedLeaseVersion=9");
    expect(calls[10]?.[1]?.method).toBe("POST");
    expect(String(calls[11]?.[0])).toBe("/api/uploads/reparse?uploadSessionId=upload%2Fid&tenantId=local-default&expectedJobVersion=8");
    expect(calls[11]?.[1]?.method).toBe("POST");
    expect(jobEventsUrl("job/id")).toBe(
      "/api/jobs/events?jobId=job%2Fid&tenantId=local-default",
    );
    expect(videoContentUrl("video/id")).toBe(
      "/api/videos/content?videoId=video%2Fid&tenantId=local-default",
    );
  });
});

describe("localized errors", () => {
  it("translates client fallbacks and hides request identifiers by default", () => {
    expect(errorMessage(new ApiClientError(503, null), "en")).toBe("Request failed (HTTP 503)");
    expect(errorMessage(new ApiClientError(400, {
      message: "原始业务错误", requestId: "request-123",
    }), "en")).toBe("原始业务错误");
    expect(errorMessage(new Error("分片上传网络连接中断"), "en")).toBe("The upload connection was interrupted");
    expect(errorMessage(null, "en")).toBe("An unknown error occurred");
    expect(errorMessage(new ApiClientError(503, null))).toBe("请求失败（HTTP 503）");
  });

  it("shows request identifiers only for the connection with a confirmed PPE display mode", async () => {
    const remote: ApiConnection = { id: "remote:ppe", baseUrl: "/remote" };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ displayMode: "debug" }))));
    await getRuntimeConfig();
    const failure = new ApiClientError(400, { message: "原始业务错误", requestId: "request-123" });
    expect(failure.requestId).toBe("request-123");
    expect(errorMessage(failure, "en")).toBe("原始业务错误 (Request ID: request-123)");
    expect(errorMessage(failure)).toBe("原始业务错误（请求编号：request-123）");
    expect(errorMessage(new ApiClientError(400, { message: "远端错误", requestId: "remote-id" }, remote))).toBe("远端错误");

    clearDataSource();
    expect(errorMessage(failure)).toBe("原始业务错误");
  });

  it("hides request identifiers again when PPE is removed or runtime configuration fails", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ displayMode: "debug" })));
    vi.stubGlobal("fetch", fetchMock);
    const failure = new ApiClientError(400, { message: "原始业务错误", requestId: "request-123" });
    await getRuntimeConfig();
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ displayMode: "release" })));
    await getRuntimeConfig();
    expect(errorMessage(failure)).toBe("原始业务错误");

    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ displayMode: "debug" })));
    await getRuntimeConfig();
    fetchMock.mockImplementation(async () => new Response(null, { status: 503 }));
    await expect(getRuntimeConfig()).rejects.toBeInstanceOf(ApiClientError);
    expect(errorMessage(failure)).toBe("原始业务错误");
  });

  it.each([false, true])("ignores an older PPE config completing after a newer config (older failure: %s)", async (olderFails) => {
    let completeOlder!: (response: Response) => void;
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { completeOlder = resolve; }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ displayMode: olderFails ? "debug" : "release" })));
    vi.stubGlobal("fetch", fetchMock);
    const older = getRuntimeConfig().catch(() => undefined);
    await getRuntimeConfig();
    completeOlder(olderFails ? new Response(null, { status: 503 }) : new Response(JSON.stringify({ displayMode: "debug" })));
    await older;
    const failure = new ApiClientError(400, { message: "原始业务错误", requestId: "request-123" });
    expect(errorMessage(failure)).toBe(olderFails ? "原始业务错误（请求编号：request-123）" : "原始业务错误");
  });
});

describe("Video Parse V2 task routes", () => {
  it("uses the V2 task identity for list, get and events", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(JSON.stringify({
      items: [], total: 0, hasMore: false, nextCursor: null,
    }), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await listVideoParseTasks("next/cursor", 25);
    await getVideoParseTask("task/id");

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "/api/jobs/video-parse/list?tenantId=local-default&limit=25&cursor=next%2Fcursor",
    );
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(
      "/api/jobs/video-parse/get?videoParseTaskId=task%2Fid&tenantId=local-default",
    );
    expect(videoParseTaskEventsUrl("task/id")).toBe(
      "/api/jobs/video-parse/events?videoParseTaskId=task%2Fid&tenantId=local-default",
    );
  });

  it("walks V2 pagination and de-duplicates task identities", async () => {
    const item = (id: string) => ({ videoParseTaskId: id });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const cursor = new URL(String(input), "http://localhost").searchParams.get("cursor");
      return new Response(JSON.stringify(cursor
        ? { items: [item("task-1"), item("task-2")], total: 2, hasMore: false, nextCursor: null }
        : { items: [item("task-1")], total: 2, hasMore: true, nextCursor: "page-2" }), {
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(listAllVideoParseTasks()).resolves.toEqual([
      item("task-1"),
      item("task-2"),
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
