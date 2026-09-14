import type {
  ApiErrorPayload,
  ParseJobView,
  SeriesCreateRequest,
  SeriesView,
  UploadCompleteView,
  UploadSessionCreateRequest,
  UploadSessionView,
  VideoChunkSearchResult,
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

  constructor(status: number, payload: ApiErrorPayload | null) {
    super(payload?.message || `请求失败（HTTP ${status}）`);
    this.name = "ApiClientError";
    this.status = status;
    this.code = payload?.code;
  }
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
  if (!response.ok) return false;
  const payload = (await response.json()) as { status?: string };
  return payload.status === "ok";
}

export function listSeries(): Promise<SeriesView[]> {
  return requestJson(`/api/series/list?workspaceId=${WORKSPACE_QUERY}`);
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
): Promise<UploadSessionView> {
  return requestJson(
    `/api/uploads/get?uploadSessionId=${encodeURIComponent(uploadSessionId)}` +
      `&workspaceId=${WORKSPACE_QUERY}`,
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

export function listJobs(): Promise<ParseJobView[]> {
  return requestJson(`/api/jobs/list?workspaceId=${WORKSPACE_QUERY}&limit=100`);
}

export function getJob(jobId: string): Promise<ParseJobView> {
  return requestJson(
    `/api/jobs/get?jobId=${encodeURIComponent(jobId)}` +
      `&workspaceId=${WORKSPACE_QUERY}`,
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

export function videoContentUrl(videoId: string): string {
  return apiUrl(
    `/api/videos/content?videoId=${encodeURIComponent(videoId)}` +
      `&workspaceId=${WORKSPACE_QUERY}`,
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
  if (error instanceof ApiClientError) return error.message;
  if (error instanceof Error) return error.message;
  return "发生了无法识别的错误";
}
