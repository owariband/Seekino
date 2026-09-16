import type {
  AgentAnswerEvent,
  AgentConversationView,
  AgentMessageEventView,
  AgentMessageSendView,
  AgentMessageView,
  ApiErrorPayload,
  EvidenceReference,
  ImageAsset,
  Page,
  ParseJobView,
  ParseUsageVO,
  SeriesCreateRequest,
  SeriesUpdateRequest,
  SeriesView,
  SeriesSummary,
  UploadCompleteView,
  UploadSessionCreateRequest,
  UploadSessionView,
  VideoChunkSearchResult,
  VideoChunkEvidence,
  VideoSearchRequest,
} from "./contracts";

export const WORKSPACE_ID =
  import.meta.env.VITE_WORKSPACE_ID?.trim() || "local-default";
const WORKSPACE_QUERY = encodeURIComponent(WORKSPACE_ID);
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(
  /\/$/,
  "",
);

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

function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
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
): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: {
      Accept: "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    throw new ApiClientError(response.status, await readError(response));
  }
  return (await response.json()) as T;
}

export async function checkHealth(): Promise<boolean> {
  const response = await fetch(apiUrl("/healthz"), {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new ApiClientError(response.status, await readError(response));
  }
  const payload = (await response.json()) as { status?: string };
  return payload.status === "ok";
}

export function listSeries(cursor?: string, limit = 100): Promise<Page<SeriesSummary>> {
  return requestJson(`/api/series/list?workspaceId=${WORKSPACE_QUERY}&limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
}

export function getSeries(seriesId: string): Promise<SeriesView> {
  return requestJson(
    `/api/series/get?seriesId=${encodeURIComponent(seriesId)}` +
      `&workspaceId=${WORKSPACE_QUERY}`,
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
  return requestJson(`/api/series/images/upload?${new URLSearchParams({ workspaceId: WORKSPACE_ID, seriesId })}`, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
    signal,
  });
}

export function imageContentUrl(imageId: string, workspaceId = WORKSPACE_ID): string {
  return apiUrl(`/api/media/images/content?${new URLSearchParams({ workspaceId, imageId })}`);
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

export function getUploadSession(
  uploadSessionId: string,
  signal?: AbortSignal,
): Promise<UploadSessionView> {
  return requestJson(
    `/api/uploads/get?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&workspaceId=${WORKSPACE_QUERY}`,
    { signal },
  );
}

export function deleteUploadSession(uploadSessionId: string): Promise<UploadSessionView> {
  return requestJson(
    `/api/uploads/delete?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&workspaceId=${WORKSPACE_QUERY}`,
    { method: "POST" },
  );
}

export function retryUploadParse(
  uploadSessionId: string,
  expectedJobVersion: number,
): Promise<ParseJobView> {
  return requestJson(
    `/api/uploads/retry-parse?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&workspaceId=${WORKSPACE_QUERY}` +
      `&expectedJobVersion=${encodeURIComponent(String(expectedJobVersion))}`,
    { method: "POST" },
  );
}

export function completeUpload(
  uploadSessionId: string,
): Promise<UploadCompleteView> {
  return requestJson(
    `/api/uploads/complete?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&workspaceId=${WORKSPACE_QUERY}`,
    { method: "POST" },
  );
}

export function listJobs(cursor?: string, limit = 100, signal?: AbortSignal): Promise<Page<ParseJobView>> {
  return requestJson(`/api/jobs/list?workspaceId=${WORKSPACE_QUERY}&limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, { signal });
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
      `&workspaceId=${WORKSPACE_QUERY}`,
  );
}

export function getParseUsage(jobId: string, signal?: AbortSignal): Promise<ParseUsageVO> {
  return requestJson(
    `/api/jobs/usage/get?workspaceId=${WORKSPACE_QUERY}&jobId=${encodeURIComponent(jobId)}`,
    { signal },
  );
}

export function jobEventsUrl(jobId: string): string {
  return apiUrl(
    `/api/jobs/events?jobId=${encodeURIComponent(jobId)}` +
      `&workspaceId=${WORKSPACE_QUERY}`,
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
    body: JSON.stringify({ workspaceId: WORKSPACE_ID }),
  });
}

export function listAgentConversations(limit = 100, cursor?: string): Promise<Page<AgentConversationView>> {
  return requestJson(`/api/agent/conversations/list?workspaceId=${WORKSPACE_QUERY}&limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
}

export function getAgentConversation(conversationId: string): Promise<AgentConversationView> {
  return requestJson(`/api/agent/conversations/get?conversationId=${encodeURIComponent(conversationId)}&workspaceId=${WORKSPACE_QUERY}`);
}

export function listAgentMessages(conversationId: string, afterSequenceNo = -1, cursor?: string): Promise<Page<AgentMessageView>> {
  return requestJson(`/api/agent/messages/list?conversationId=${encodeURIComponent(conversationId)}&workspaceId=${WORKSPACE_QUERY}&afterSequenceNo=${afterSequenceNo}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
}

export function getAgentMessage(conversationId: string, identity: { messageId: string } | { idempotencyKey: string }): Promise<AgentMessageSendView> {
  const query = new URLSearchParams({ workspaceId: WORKSPACE_ID, conversationId, ...identity });
  return requestJson(`/api/agent/messages/get?${query}`);
}

export function getVideoChunk(videoChunkId: string, workspaceId = WORKSPACE_ID): Promise<VideoChunkEvidence> {
  return requestJson(`/api/video-chunks/get?${new URLSearchParams({ workspaceId, videoChunkId })}`);
}

export function mediaUrl(url: string | null): string | null {
  if (!url) return null;
  if (/^https?:\/\//i.test(url)) return url;
  return url.startsWith("/") && !url.startsWith("//") ? apiUrl(url) : null;
}

export function agentMessagesEventsUrl(conversationId: string): string {
  return apiUrl(`/api/agent/messages/events?conversationId=${encodeURIComponent(conversationId)}&workspaceId=${WORKSPACE_QUERY}`);
}

export function subscribeAgentMessages(
  conversationId: string,
  onSnapshot: (snapshot: AgentMessageEventView) => void,
): () => void {
  const source = new EventSource(agentMessagesEventsUrl(conversationId));
  source.addEventListener("messages", (event) => {
    let snapshot: AgentMessageEventView;
    try { snapshot = JSON.parse((event as MessageEvent<string>).data) as AgentMessageEventView; }
    catch { return; }
    if (snapshot?.conversationId === conversationId && Array.isArray(snapshot.messages)) onSnapshot(snapshot);
  });
  return () => source.close();
}

export function sendAgentMessage(
  conversationId: string,
  content: string,
  idempotencyKey: string,
  seriesIds: string[] = [],
  topK = 10,
  reference: EvidenceReference | null = null,
): Promise<AgentMessageSendView> {
  return requestJson(`/api/agent/messages/send?conversationId=${encodeURIComponent(conversationId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ workspaceId: WORKSPACE_ID, content, seriesIds, topK, reference }),
  });
}

export async function streamAgentMessage(
  conversationId: string,
  content: string,
  idempotencyKey: string,
  onEvent: (event: AgentAnswerEvent) => void,
  signal: AbortSignal,
  seriesIds: string[] = [],
  topK = 10,
  reference: EvidenceReference | null = null,
): Promise<void> {
  const response = await fetch(apiUrl(`/api/agent/messages/send?conversationId=${encodeURIComponent(conversationId)}`), {
    method: "POST",
    headers: { Accept: "text/event-stream", "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ workspaceId: WORKSPACE_ID, content, seriesIds, topK, reference }),
    signal,
  });
  if (!response.ok) throw new ApiClientError(response.status, await readError(response));
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

export function videoContentUrl(videoId: string, workspaceId = WORKSPACE_ID): string {
  return apiUrl(
    `/api/videos/content?videoId=${encodeURIComponent(videoId)}` +
      `&workspaceId=${encodeURIComponent(workspaceId)}`,
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
          `&segmentNumber=${segmentNumber}&workspaceId=${WORKSPACE_QUERY}`,
      ),
    );
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader(
      "Content-Range",
      `bytes ${start}-${endExclusive - 1}/${file.size}`,
    );
    xhr.setRequestHeader("X-Content-SHA256", checksum);
    xhr.upload.onprogress = (event) => onProgress(event.loaded);
    xhr.onload = () => {
      signal.removeEventListener("abort", abort);
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
      reject(new ApiClientError(xhr.status, payload));
    };
    xhr.onerror = () => {
      signal.removeEventListener("abort", abort);
      reject(new Error("分片上传网络连接中断"));
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
