import type {
  AgentAnswerEvent,
  AgentConversationView,
  AgentGlobalSettings,
  AgentGlobalSettingsValues,
  AgentMessageEventView,
  AgentMessageOptions,
  AgentMessageRunOptions,
  AgentMessageSendView,
  AgentMessageView,
  ApiErrorPayload,
  ApiConnection,
  AuthIdentity,
  DataSource,
  EvidenceReference,
  ImageAsset,
  Page,
  ParseConfig,
  ParseJobView,
  RuntimeConfig,
  UsageOverviewOptions,
  UsageOverviewVO,
  SeriesCreateRequest,
  SeriesUpdateRequest,
  SeriesView,
  SeriesSummary,
  UploadCompleteView,
  UploadSessionCreateRequest,
  UploadSessionView,
  VideoEpisodeUpdateRequest,
  VideoChunkSearchResult,
  VideoChunkEvidence,
  VideoSummary,
  VideoParseTaskView,
  VideoSearchRequest,
} from "./contracts";

export let TENANT_ID =
  import.meta.env.VITE_TENANT_ID?.trim() || "local-default";
let TENANT_QUERY = encodeURIComponent(TENANT_ID);
let activeDataSource: DataSource | null = null;
let scopeController = new AbortController();
const accountIdentities = new Map<string, AuthIdentity | null>();
const eventSources = new Set<EventSource>();
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(
  /\/$/,
  "",
);
export const PRIMARY_CONNECTION: ApiConnection = {
  id: `primary:${API_BASE_URL || "same-origin"}`,
  baseUrl: API_BASE_URL,
};
const remoteBaseUrl = import.meta.env.VITE_REMOTE_API_BASE_URL?.trim().replace(/\/$/, "");
export const REMOTE_CONNECTION: ApiConnection | null = remoteBaseUrl ? {
  id: `remote:${import.meta.env.VITE_REMOTE_API_ORIGIN || remoteBaseUrl}`,
  baseUrl: remoteBaseUrl,
} : null;

interface RequestContext {
  connection: ApiConnection;
  identity: AuthIdentity | null;
  scopeSignal?: AbortSignal;
}

function businessContext(): RequestContext {
  const connection = activeDataSource?.connection ?? PRIMARY_CONNECTION;
  return {
    connection,
    identity: activeDataSource?.identity ?? null,
    scopeSignal: scopeController.signal,
  };
}

function accountContext(connection: ApiConnection): RequestContext {
  return { connection, identity: accountIdentities.get(connection.id) ?? null };
}

function authRequired(connection: ApiConnection): void {
  window.dispatchEvent(new CustomEvent("anivag-auth-required", {
    detail: { connectionId: connection.id },
  }));
}

export class ApiClientError extends Error {
  readonly status: number;
  readonly code: string | undefined;
  readonly requestId: string | undefined;

  constructor(status: number, payload: ApiErrorPayload | null) {
    super(payload?.message || `请求失败（HTTP ${status}）`);
    this.name = "ApiClientError";
    this.status = status;
    this.code = payload?.code;
    this.requestId = payload?.requestId;
  }
}

export function shouldRetryApiRequest(
  failureCount: number,
  error: unknown,
): boolean {
  if (error instanceof ApiClientError && error.status < 500) return false;
  return failureCount < 1;
}

function apiUrl(path: string, connection = activeDataSource?.connection ?? PRIMARY_CONNECTION): string {
  return `${connection.baseUrl}${path}`;
}

async function readError(response: Response): Promise<ApiErrorPayload | null> {
  try {
    return (await response.json()) as ApiErrorPayload;
  } catch {
    return null;
  }
}

async function requestJson<T>(
  path: string,
  init?: RequestInit,
  context = businessContext(),
): Promise<T> {
  const signal = scopedSignal(init?.signal, context.scopeSignal);
  signal?.throwIfAborted();
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (context.identity?.csrfToken && !["GET", "HEAD"].includes(init?.method ?? "GET")) {
    headers.set("X-CSRF-Token", context.identity.csrfToken);
  }
  const response = await fetch(apiUrl(path, context.connection), {
    ...init,
    credentials: "include",
    headers,
    signal,
  });
  signal?.throwIfAborted();
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/api/auth/")) authRequired(context.connection);
    const payload = await readError(response);
    signal?.throwIfAborted();
    throw new ApiClientError(response.status, payload);
  }
  const payload = (await response.json()) as T;
  signal?.throwIfAborted();
  return payload;
}

function scopedSignal(signal?: AbortSignal | null, scopeSignal?: AbortSignal): AbortSignal | undefined {
  if (!scopeSignal) return signal ?? undefined;
  return signal ? AbortSignal.any([scopeSignal, signal]) : scopeSignal;
}

export function configureAccountIdentity(connection: ApiConnection, identity: AuthIdentity | null): void {
  accountIdentities.set(connection.id, identity);
}

export function clearDataSource(): void {
  scopeController.abort();
  eventSources.forEach((stream) => stream.close());
  eventSources.clear();
  activeDataSource = null;
}

export function configureDataSource(source: DataSource): void {
  const previous = activeDataSource;
  if (!previous || previous.key !== source.key || previous.connection.id !== source.connection.id
    || previous.connection.baseUrl !== source.connection.baseUrl || previous.tenantId !== source.tenantId
    || previous.identity.userId !== source.identity.userId || previous.identity.csrfToken !== source.identity.csrfToken
    || previous.identity.permissions.canUpload !== source.identity.permissions.canUpload
    || previous.identity.permissions.canManage !== source.identity.permissions.canManage
    || previous.identity.permissions.canConfigure !== source.identity.permissions.canConfigure) {
    scopeController.abort();
    eventSources.forEach((stream) => stream.close());
    eventSources.clear();
    scopeController = new AbortController();
  }
  activeDataSource = source;
  TENANT_ID = source.tenantId;
  TENANT_QUERY = encodeURIComponent(source.tenantId);
}

export function configureApiIdentity(identity: AuthIdentity, tenantId: string): void {
  configureAccountIdentity(PRIMARY_CONNECTION, identity);
  configureDataSource({
    key: `${PRIMARY_CONNECTION.id}:${identity.userId}:${tenantId}`,
    kind: identity.mode === "local" ? "local" : tenantId === identity.publicTenantId ? "public" : "personal",
    connection: PRIMARY_CONNECTION,
    tenantId,
    identity,
  });
}

export function identityStorageKey(prefix: string, localKey = `${prefix}:${TENANT_ID}`): string {
  return activeDataSource && activeDataSource.kind !== "local"
    ? `${prefix}:${encodeURIComponent(activeDataSource.connection.id)}:${activeDataSource.identity.userId}:${TENANT_ID}`
    : localKey;
}

export function getAuthIdentity(signal?: AbortSignal, connection = REMOTE_CONNECTION ?? PRIMARY_CONNECTION): Promise<AuthIdentity> {
  return requestJson("/api/auth/me", { signal }, accountContext(connection));
}

export function createGuestIdentity(signal?: AbortSignal, connection = REMOTE_CONNECTION ?? PRIMARY_CONNECTION): Promise<AuthIdentity> {
  return requestJson("/api/auth/guest", { method: "POST", signal }, accountContext(connection));
}

export function sendEmailCode(email: string, connection = REMOTE_CONNECTION ?? PRIMARY_CONNECTION): Promise<{ retryAfterSeconds: number }> {
  return requestJson("/api/auth/email/send", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }),
  }, accountContext(connection));
}

export function verifyEmailCode(email: string, code: string, connection = REMOTE_CONNECTION ?? PRIMARY_CONNECTION): Promise<AuthIdentity> {
  return requestJson("/api/auth/email/verify", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, code }),
  }, accountContext(connection));
}

export function logoutIdentity(connection = REMOTE_CONNECTION ?? PRIMARY_CONNECTION): Promise<AuthIdentity> {
  return requestJson("/api/auth/logout", { method: "POST" }, accountContext(connection));
}

export async function checkHealth(): Promise<boolean> {
  const context = businessContext();
  const signal = context.scopeSignal;
  signal?.throwIfAborted();
  const response = await fetch(apiUrl("/healthz", context.connection), {
    credentials: "include",
    headers: { Accept: "application/json" },
    signal,
  });
  signal?.throwIfAborted();
  if (!response.ok) {
    const payload = await readError(response);
    signal?.throwIfAborted();
    throw new ApiClientError(response.status, payload);
  }
  const payload = (await response.json()) as { status?: string };
  signal?.throwIfAborted();
  return payload.status === "ok";
}

export function getRuntimeConfig(signal?: AbortSignal): Promise<RuntimeConfig> {
  return requestJson("/api/system/runtime-config", { signal, cache: "no-store" });
}

export function listSeries(cursor?: string, limit = 100): Promise<Page<SeriesSummary>> {
  return requestJson(`/api/series/list?tenantId=${TENANT_QUERY}&limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
}

export function getSeries(seriesId: string): Promise<SeriesView> {
  return requestJson(
    `/api/series/get?seriesId=${encodeURIComponent(seriesId)}` +
      `&tenantId=${TENANT_QUERY}`,
  );
}

export function createSeries(body: SeriesCreateRequest): Promise<SeriesView> {
  return requestJson("/api/series/create", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function updateSeries(body: SeriesUpdateRequest): Promise<SeriesView> {
  return requestJson("/api/series/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function uploadSeriesImage(seriesId: string, file: File, signal?: AbortSignal): Promise<ImageAsset> {
  return requestJson(`/api/series/images/upload?${new URLSearchParams({ tenantId: TENANT_ID, seriesId })}`, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
    signal,
  });
}

export function updateVideoEpisode(body: VideoEpisodeUpdateRequest): Promise<VideoSummary> {
  return requestJson("/api/videos/update_episode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function imageContentUrl(imageId: string, tenantId = TENANT_ID): string {
  return apiUrl(`/api/media/images/content?${new URLSearchParams({ tenantId, imageId })}`);
}

export function createUploadSession(
  seriesId: string,
  idempotencyKey: string,
  body: UploadSessionCreateRequest,
): Promise<UploadSessionView> {
  return requestJson(
    `/api/uploads/create?seriesId=${encodeURIComponent(seriesId)}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(body),
    },
  );
}

export function getParseConfig(signal?: AbortSignal): Promise<ParseConfig> {
  return requestJson("/api/uploads/parse-config", { signal });
}

export function getUploadSession(
  uploadSessionId: string,
  signal?: AbortSignal,
): Promise<UploadSessionView> {
  return requestJson(
    `/api/uploads/get?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&tenantId=${TENANT_QUERY}`,
    { signal },
  );
}

export function deleteUploadSession(uploadSessionId: string): Promise<UploadSessionView> {
  return requestJson(
    `/api/uploads/delete?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&tenantId=${TENANT_QUERY}`,
    { method: "POST" },
  );
}

export function retryUploadParse(
  uploadSessionId: string,
  expectedJobVersion: number,
): Promise<VideoParseTaskView> {
  return requestJson(
    `/api/uploads/retry-parse?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&tenantId=${TENANT_QUERY}` +
      `&expectedJobVersion=${encodeURIComponent(String(expectedJobVersion))}`,
    { method: "POST" },
  );
}

export function reparseUpload(
  uploadSessionId: string,
  expectedJobVersion: number,
): Promise<VideoParseTaskView> {
  return requestJson(
    `/api/uploads/reparse?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&tenantId=${TENANT_QUERY}` +
      `&expectedJobVersion=${encodeURIComponent(String(expectedJobVersion))}`,
    { method: "POST" },
  );
}

export function completeUpload(
  uploadSessionId: string,
): Promise<UploadCompleteView> {
  return requestJson(
    `/api/uploads/complete?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&tenantId=${TENANT_QUERY}`,
    { method: "POST" },
  );
}

export function listJobs(cursor?: string, limit = 100, signal?: AbortSignal): Promise<Page<ParseJobView>> {
  return requestJson(`/api/jobs/list?tenantId=${TENANT_QUERY}&limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, { signal });
}

export async function listAllJobs(signal?: AbortSignal): Promise<ParseJobView[]> {
  const items = new Map<string, ParseJobView>();
  const cursors = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await listJobs(cursor, 100, signal);
    for (const job of page.items) items.set(job.jobId, job);
    if (page.hasMore && !page.nextCursor) throw new Error("任务列表缺少下一页游标");
    cursor = page.hasMore ? page.nextCursor! : undefined;
    if (cursor && cursors.has(cursor)) throw new Error("任务列表返回了重复的分页游标");
    if (cursor) cursors.add(cursor);
  } while (cursor);
  return [...items.values()];
}

export function getJob(jobId: string): Promise<ParseJobView> {
  return requestJson(
    `/api/jobs/get?jobId=${encodeURIComponent(jobId)}` +
      `&tenantId=${TENANT_QUERY}`,
  );
}

export function listVideoParseTasks(
  cursor?: string,
  limit = 100,
  signal?: AbortSignal,
): Promise<Page<VideoParseTaskView>> {
  return requestJson(
    `/api/jobs/video-parse/list?tenantId=${TENANT_QUERY}&limit=${limit}` +
      `${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    { signal },
  );
}

export async function listAllVideoParseTasks(
  signal?: AbortSignal,
): Promise<VideoParseTaskView[]> {
  const items = new Map<string, VideoParseTaskView>();
  const cursors = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await listVideoParseTasks(cursor, 100, signal);
    for (const task of page.items) items.set(task.videoParseTaskId, task);
    if (page.hasMore && !page.nextCursor) {
      throw new Error("V2 解析任务列表缺少下一页游标");
    }
    cursor = page.hasMore ? page.nextCursor! : undefined;
    if (cursor && cursors.has(cursor)) {
      throw new Error("V2 解析任务列表返回了重复的分页游标");
    }
    if (cursor) cursors.add(cursor);
  } while (cursor);
  return [...items.values()];
}

export function getVideoParseTask(
  videoParseTaskId: string,
): Promise<VideoParseTaskView> {
  return requestJson(
    `/api/jobs/video-parse/get?videoParseTaskId=${encodeURIComponent(videoParseTaskId)}` +
      `&tenantId=${TENANT_QUERY}`,
  );
}

export function getUsageOverview(options: UsageOverviewOptions = {}, signal?: AbortSignal): Promise<UsageOverviewVO> {
  const query = new URLSearchParams({ tenantId: TENANT_ID });
  for (const [key, value] of Object.entries(options)) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  return requestJson(
    `/api/usage/overview?${query}`,
    { signal },
  );
}

export function jobEventsUrl(jobId: string): string {
  return apiUrl(
    `/api/jobs/events?jobId=${encodeURIComponent(jobId)}` +
      `&tenantId=${TENANT_QUERY}`,
  );
}

export function videoParseTaskEventsUrl(videoParseTaskId: string): string {
  return apiUrl(
    `/api/jobs/video-parse/events?videoParseTaskId=${encodeURIComponent(videoParseTaskId)}` +
      `&tenantId=${TENANT_QUERY}`,
  );
}

export function searchVideos(
  body: VideoSearchRequest,
): Promise<VideoChunkSearchResult> {
  return requestJson("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function createAgentConversation(
  idempotencyKey: string,
): Promise<AgentConversationView> {
  return requestJson("/api/agent/conversations/create", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ tenantId: TENANT_ID }),
  });
}

export function listAgentConversations(limit = 100, cursor?: string): Promise<Page<AgentConversationView>> {
  return requestJson(`/api/agent/conversations/list?tenantId=${TENANT_QUERY}&limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
}

export function getAgentConversation(conversationId: string): Promise<AgentConversationView> {
  return requestJson(`/api/agent/conversations/get?conversationId=${encodeURIComponent(conversationId)}&tenantId=${TENANT_QUERY}`);
}

export function deleteAgentConversation(conversationId: string): Promise<AgentConversationView> {
  return requestJson(`/api/agent/conversations/delete?conversationId=${encodeURIComponent(conversationId)}&tenantId=${TENANT_QUERY}`, {
    method: "POST",
  });
}

export function listAgentMessages(conversationId: string, afterSequenceNo = -1, cursor?: string): Promise<Page<AgentMessageView>> {
  return requestJson(`/api/agent/messages/list?conversationId=${encodeURIComponent(conversationId)}&tenantId=${TENANT_QUERY}&afterSequenceNo=${afterSequenceNo}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
}

export function getAgentMessage(conversationId: string, identity: { messageId: string } | { idempotencyKey: string }): Promise<AgentMessageSendView> {
  const query = new URLSearchParams({ tenantId: TENANT_ID, conversationId, ...identity });
  return requestJson(`/api/agent/messages/get?${query}`);
}

export function getAgentMessageOptions(): Promise<AgentMessageOptions> {
  return requestJson("/api/agent/messages/options");
}

export function getAgentGlobalSettings(): Promise<AgentGlobalSettings> {
  return requestJson("/api/agent/global-settings");
}

export function updateAgentGlobalSettings(
  values: Partial<AgentGlobalSettingsValues>,
  revision: string,
): Promise<AgentGlobalSettings> {
  return requestJson("/api/agent/global-settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values, revision }),
  });
}

export function getVideoChunk(videoChunkId: string, tenantId = TENANT_ID): Promise<VideoChunkEvidence> {
  return requestJson(`/api/video-chunks/get?${new URLSearchParams({ tenantId, videoChunkId })}`);
}

export function mediaUrl(url: string | null): string | null {
  if (!url) return null;
  if (/^https?:\/\//i.test(url)) {
    const connection = activeDataSource?.connection ?? PRIMARY_CONNECTION;
    const upstream = connection.id === REMOTE_CONNECTION?.id ? import.meta.env.VITE_REMOTE_API_ORIGIN : undefined;
    if (upstream) {
      let parsed: URL;
      try { parsed = new URL(url); } catch { return null; }
      if (parsed.origin === upstream && parsed.pathname.startsWith("/api/")) {
        return apiUrl(`${parsed.pathname}${parsed.search}${parsed.hash}`, connection);
      }
    }
    return url;
  }
  return url.startsWith("/") && !url.startsWith("//") ? apiUrl(url) : null;
}

export function agentMessagesEventsUrl(conversationId: string): string {
  return apiUrl(`/api/agent/messages/events?conversationId=${encodeURIComponent(conversationId)}&tenantId=${TENANT_QUERY}`);
}

export function subscribeAgentMessages(
  conversationId: string,
  onSnapshot: (snapshot: AgentMessageEventView) => void,
): () => void {
  const context = businessContext();
  const signal = context.scopeSignal;
  signal?.throwIfAborted();
  const source = new EventSource(agentMessagesEventsUrl(conversationId), { withCredentials: true });
  eventSources.add(source);
  source.addEventListener("messages", (event) => {
    if (signal?.aborted) return;
    let snapshot: AgentMessageEventView;
    try { snapshot = JSON.parse((event as MessageEvent<string>).data) as AgentMessageEventView; }
    catch { return; }
    if (snapshot?.conversationId === conversationId && Array.isArray(snapshot.messages)) onSnapshot(snapshot);
  });
  source.addEventListener("error", () => {
    if (!signal?.aborted) authRequired(context.connection);
  });
  return () => { eventSources.delete(source); source.close(); };
}

export function sendAgentMessage(
  conversationId: string,
  content: string,
  idempotencyKey: string,
  seriesIds: string[] = [],
  reference: EvidenceReference | null = null,
  options: AgentMessageRunOptions = {},
  extraContext: Record<string, unknown> | null = null,
): Promise<AgentMessageSendView> {
  return requestJson(`/api/agent/messages/send?conversationId=${encodeURIComponent(conversationId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({
      tenantId: TENANT_ID,
      content,
      seriesIds,
      reference,
      ...options,
      ...(extraContext === null ? {} : { extraContext }),
    }),
  });
}

export async function streamAgentMessage(
  conversationId: string,
  content: string,
  idempotencyKey: string,
  onEvent: (event: AgentAnswerEvent) => void,
  signal: AbortSignal,
  seriesIds: string[] = [],
  reference: EvidenceReference | null = null,
  options: AgentMessageRunOptions = {},
  extraContext: Record<string, unknown> | null = null,
): Promise<void> {
  const context = businessContext();
  signal = scopedSignal(signal, context.scopeSignal)!;
  signal.throwIfAborted();
  const response = await fetch(apiUrl(`/api/agent/messages/send?conversationId=${encodeURIComponent(conversationId)}`, context.connection), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "text/event-stream", "Content-Type": "application/json", "Idempotency-Key": idempotencyKey,
      ...(context.identity?.csrfToken ? { "X-CSRF-Token": context.identity.csrfToken } : {}),
    },
    body: JSON.stringify({
      tenantId: TENANT_ID,
      content,
      seriesIds,
      reference,
      ...options,
      ...(extraContext === null ? {} : { extraContext }),
    }),
    signal,
  });
  signal.throwIfAborted();
  if (!response.ok) {
    if (response.status === 401) authRequired(context.connection);
    const payload = await readError(response);
    signal.throwIfAborted();
    throw new ApiClientError(response.status, payload);
  }
  if (!response.body || !response.headers.get("Content-Type")?.includes("text/event-stream")) {
    await response.body?.cancel().catch(() => undefined);
    throw new Error("服务未返回回答事件流");
  }
  await consumeEventStream(response.body, (type, data) => {
    if (!type.startsWith("answer.")) return;
    const event = { ...JSON.parse(data), type } as AgentAnswerEvent;
    if (!Number.isInteger(event.sequence) || event.sequence <= 0) return;
    if (event.conversationId === conversationId) onEvent(event);
  }, signal);
}

async function consumeEventStream(
  body: ReadableStream<Uint8Array>,
  onEvent: (type: string, data: string) => void,
  signal: AbortSignal,
): Promise<void> {
  signal.throwIfAborted();
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let pending = "";
  let type = "message";
  let data: string[] = [];
  let ended = false;
  const dispatch = () => {
    signal.throwIfAborted();
    if (data.length) onEvent(type, data.join("\n"));
    type = "message";
    data = [];
  };
  const line = (value: string) => {
    if (!value) { dispatch(); return; }
    if (value.startsWith(":")) return;
    const colon = value.indexOf(":");
    const field = colon < 0 ? value : value.slice(0, colon);
    let content = colon < 0 ? "" : value.slice(colon + 1);
    if (content.startsWith(" ")) content = content.slice(1);
    if (field === "event") type = content;
    if (field === "data") data.push(content);
  };
  const drain = (final: boolean) => {
    let start = 0;
    for (let index = 0; index < pending.length; index++) {
      const char = pending[index];
      if (char !== "\r" && char !== "\n") continue;
      if (char === "\r" && index === pending.length - 1 && !final) break;
      line(pending.slice(start, index));
      if (char === "\r" && pending[index + 1] === "\n") index++;
      start = index + 1;
    }
    pending = pending.slice(start);
    if (final) {
      if (pending) line(pending);
      dispatch();
    }
  };
  const abort = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener("abort", abort, { once: true });
  try {
    while (true) {
      signal.throwIfAborted();
      const { value, done } = await reader.read();
      signal.throwIfAborted();
      pending += decoder.decode(value, { stream: !done });
      drain(done);
      if (done) { ended = true; break; }
    }
  } finally {
    signal.removeEventListener("abort", abort);
    if (!ended) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

export function videoContentUrl(videoId: string, tenantId = TENANT_ID): string {
  return apiUrl(
    `/api/videos/content?videoId=${encodeURIComponent(videoId)}` +
      `&tenantId=${encodeURIComponent(tenantId)}`,
  );
}

export async function sha256(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    await blob.arrayBuffer(),
  );
  return Array.from(new Uint8Array(digest), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
}

interface UploadSegmentOptions {
  file: File;
  session: UploadSessionView;
  segmentNumber: number;
  signal: AbortSignal;
  onProgress: (loadedBytes: number) => void;
}

export async function uploadSegment({
  file,
  session,
  segmentNumber,
  signal,
  onProgress,
}: UploadSegmentOptions): Promise<number> {
  const context = businessContext();
  const tenantQuery = TENANT_QUERY;
  signal = scopedSignal(signal, context.scopeSignal)!;
  if (signal.aborted) {
    throw new DOMException("Upload paused", "AbortError");
  }
  const start = segmentNumber * session.preferredSegmentSizeBytes;
  const endExclusive = Math.min(
    start + session.preferredSegmentSizeBytes,
    file.size,
  );
  const body = file.slice(start, endExclusive);
  const checksum = await sha256(body);

  if (signal.aborted) {
    throw new DOMException("Upload paused", "AbortError");
  }

  return new Promise<number>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    signal.addEventListener("abort", abort, { once: true });

    xhr.open(
      "PUT",
      apiUrl(
        "/api/uploads/segments/upload" +
          `?uploadSessionId=${encodeURIComponent(session.uploadSessionId)}` +
          `&segmentNumber=${segmentNumber}&tenantId=${tenantQuery}`,
        context.connection,
      ),
    );
    xhr.withCredentials = true;
    if (context.identity?.csrfToken) xhr.setRequestHeader("X-CSRF-Token", context.identity.csrfToken);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader(
      "Content-Range",
      `bytes ${start}-${endExclusive - 1}/${file.size}`,
    );
    xhr.setRequestHeader("X-Content-SHA256", checksum);
    xhr.upload.onprogress = (event) => { if (!signal.aborted) onProgress(event.loaded); };
    xhr.onload = () => {
      signal.removeEventListener("abort", abort);
      if (signal.aborted) {
        reject(new DOMException("Upload paused", "AbortError"));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(body.size);
        resolve(body.size);
        return;
      }
      let payload: ApiErrorPayload | null = null;
      try {
        payload = JSON.parse(xhr.responseText) as ApiErrorPayload;
      } catch {
        payload = null;
      }
      if (xhr.status === 401) authRequired(context.connection);
      reject(new ApiClientError(xhr.status, payload));
    };
    xhr.onerror = () => {
      signal.removeEventListener("abort", abort);
      reject(signal.aborted ? new DOMException("Upload paused", "AbortError") : new Error("分片上传网络连接中断"));
    };
    xhr.onabort = () => {
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Upload paused", "AbortError"));
    };
    xhr.send(body);
  });
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiClientError) {
    return error.requestId ? `${error.message}（请求编号：${error.requestId}）` : error.message;
  }
  if (error instanceof Error) return error.message;
  return "发生了无法识别的错误";
}
