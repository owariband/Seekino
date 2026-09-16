import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError } from "../../api/client";
import type { ParseJobView, UploadSessionView } from "../../api/contracts";
import { UploadDialog } from "./UploadDialog";
import { UploadProvider, useUploads } from "./UploadProvider";

const mocks = vi.hoisted(() => ({
  selectedSeries: { seriesId: "series-a", name: "测试系列", videos: [], pendingUploads: [] as UploadSessionView[] },
  refreshSeries: vi.fn(),
  createUploadSession: vi.fn(),
  getUploadSession: vi.fn(),
  deleteUploadSession: vi.fn(),
  completeUpload: vi.fn(),
  uploadSegment: vi.fn(),
}));
vi.mock("../../app/workspace", () => ({ useWorkspace: () => ({ ...mocks, mode: "live" }) }));
vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  createUploadSession: mocks.createUploadSession,
  getUploadSession: mocks.getUploadSession,
  deleteUploadSession: mocks.deleteUploadSession,
  completeUpload: mocks.completeUpload,
  uploadSegment: mocks.uploadSegment,
}));

const file = new File(["abcdefghij"], "episode.mp4", { type: "video/mp4" });
const session: UploadSessionView = {
  uploadSessionId: "session-a", workspaceId: "local-default", seriesId: "series-a",
  episodeOrder: 1, uploadSequence: 1, originalFilename: file.name, mediaType: file.type,
  totalSizeBytes: 10, contentSha256: null, preferredSegmentSizeBytes: 4, totalSegments: 3,
  status: "UPLOADING", uploadedSegmentCount: 0, uploadedSizeBytes: 0,
  missingSegmentNumbers: [0, 1, 2], completionStage: null, completionProgress: null,
  estimatedRemainingMs: null, expiresAt: 100, videoId: null, jobId: null, lastError: null,
  version: 1, createdAt: 1, updatedAt: 1,
};

const retriedJob: ParseJobView = {
  jobId: "job-a", uploadSessionId: session.uploadSessionId,
  workspaceId: "local-default", seriesId: session.seriesId, videoId: "video-a",
  seriesName: "测试系列", episodeOrder: 1, originalFilename: file.name,
  pipelineVersion: "m1", retryOfJobId: null, status: "QUEUED", stage: "QUEUED",
  progress: 18, attemptCount: 3, maxAttempts: 4, availableAt: 1,
  startedAt: 1, completedAt: null, estimatedRemainingMs: null,
  canRetry: false, canCancel: false, lastError: null, version: 8,
  checkpoints: [], createdAt: 1, updatedAt: 2,
};

function Controls() {
  const {
    enqueue,
    forgetDeletedSession,
    openDialog,
    syncRetriedParse,
  } = useUploads();
  return <>
    <button onClick={() => { enqueue([{ file, episodeOrder: 99 }]); openDialog(); }}>开始</button>
    <button onClick={openDialog}>上传面板</button>
    <button onClick={() => forgetDeletedSession(session.uploadSessionId)}>同步外部删除</button>
    <button onClick={() => syncRetriedParse(retriedJob)}>同步解析重试</button>
    <UploadDialog />
  </>;
}

beforeEach(() => {
  vi.unstubAllGlobals();
  mocks.selectedSeries.pendingUploads = [];
  mocks.refreshSeries.mockReset().mockResolvedValue(undefined);
  mocks.createUploadSession.mockReset().mockResolvedValue(session);
  mocks.getUploadSession.mockReset().mockResolvedValue(session);
  mocks.deleteUploadSession.mockReset().mockResolvedValue({ ...session, status: "EXPIRED" });
  mocks.completeUpload.mockReset();
  mocks.uploadSegment.mockReset().mockImplementation(({ signal }: { signal: AbortSignal }) =>
    new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }),
  );
});

describe("upload deletion", () => {
  it("waits for a pending session creation, deletes it and never starts uploading", async () => {
    let finish!: (value: UploadSessionView) => void;
    mocks.createUploadSession.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "开始" }));
    await user.click(screen.getByRole("button", { name: `删除 ${file.name}` }));
    expect(screen.getByText(/正在删除/)).toBeInTheDocument();
    expect(mocks.deleteUploadSession).not.toHaveBeenCalled();
    await act(async () => finish(session));
    await waitFor(() => expect(mocks.deleteUploadSession).toHaveBeenCalledWith(session.uploadSessionId));
    await waitFor(() => expect(screen.queryByText(file.name)).not.toBeInTheDocument());
    expect(mocks.uploadSegment).not.toHaveBeenCalled();
    expect(mocks.completeUpload).not.toHaveBeenCalled();
  });

  it("aborts in-flight segments, keeps a failed deletion visible and permits retry", async () => {
    mocks.deleteUploadSession.mockRejectedValueOnce(new ApiClientError(503, { message: "暂时不可用", requestId: "request-a" }));
    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "开始" }));
    await waitFor(() => expect(mocks.uploadSegment).toHaveBeenCalledTimes(3));
    const options = mocks.uploadSegment.mock.calls.map(([value]) => value as { signal: AbortSignal; onProgress: (bytes: number) => void });
    expect(screen.getByText(file.name).closest("article")).toHaveTextContent("01");
    await user.click(screen.getByRole("button", { name: `删除 ${file.name}` }));
    expect(await screen.findByText(/删除失败.*request-a/)).toBeInTheDocument();
    expect(options.every((option) => option.signal.aborted)).toBe(true);
    act(() => options.forEach((option) => option.onProgress(10)));
    expect(screen.getByText(/删除失败/)).toBeInTheDocument();
    expect(mocks.completeUpload).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: `删除 ${file.name}` }));
    await waitFor(() => expect(screen.queryByText(file.name)).not.toBeInTheDocument());
    expect(mocks.deleteUploadSession).toHaveBeenCalledTimes(2);
  });

  it("recovers unfinished sessions after reload, resumes after reselecting the original file, and remains deletable", async () => {
    mocks.selectedSeries.pendingUploads = [session];
    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "上传面板" }));
    expect(screen.getByText(file.name)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: `继续 ${file.name}` })).not.toBeInTheDocument();
    await user.upload(screen.getByLabelText(`选择原文件继续 ${file.name}`), file);
    await waitFor(() => expect(mocks.uploadSegment).toHaveBeenCalledTimes(3));
    expect(mocks.getUploadSession).toHaveBeenCalledWith(session.uploadSessionId, expect.any(AbortSignal));
    await user.click(screen.getByRole("button", { name: `删除 ${file.name}` }));
    await waitFor(() => expect(screen.queryByText(file.name)).not.toBeInTheDocument());
    expect(mocks.deleteUploadSession).toHaveBeenCalledWith(session.uploadSessionId);
    expect(mocks.createUploadSession).not.toHaveBeenCalled();
  });

  it("removes a recovered task from memory after the same aggregate is deleted elsewhere", async () => {
    mocks.selectedSeries.pendingUploads = [session];
    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "上传面板" }));
    expect(screen.getByText(file.name)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "同步外部删除" }));

    await waitFor(() => expect(screen.queryByText(file.name)).not.toBeInTheDocument());
    expect(mocks.deleteUploadSession).not.toHaveBeenCalled();
  });

  it("reattaches a recovered in-memory task when its parse job is manually retried", async () => {
    mocks.selectedSeries.pendingUploads = [session];
    vi.stubGlobal("EventSource", class {
      onerror: (() => void) | null = null;
      addEventListener() {}
      close() {}
    });
    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "上传面板" }));
    expect(screen.getByText(file.name)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "同步解析重试" }));

    await waitFor(() => expect(screen.getByText(file.name).closest("article")).toHaveTextContent("解析 · QUEUED"));
  });

  it("retains a session created during pause and resumes without allocating another order", async () => {
    let finish!: (value: UploadSessionView) => void;
    mocks.createUploadSession.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "开始" }));
    await user.click(screen.getByRole("button", { name: `暂停 ${file.name}` }));
    await act(async () => finish(session));
    expect(screen.getByText(/已暂停/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: `继续 ${file.name}` }));
    await waitFor(() => expect(mocks.uploadSegment).toHaveBeenCalledTimes(3));
    expect(mocks.createUploadSession).toHaveBeenCalledTimes(1);
    expect(mocks.getUploadSession).toHaveBeenCalledWith(session.uploadSessionId, expect.any(AbortSignal));
    await user.click(screen.getByRole("button", { name: `删除 ${file.name}` }));
    await waitFor(() => expect(screen.queryByText(file.name)).not.toBeInTheDocument());
  });

  it("recovers a completing session and retries server completion without the original file", async () => {
    const completing = {
      ...session,
      status: "COMPLETING" as const,
      uploadedSegmentCount: 3,
      uploadedSizeBytes: session.totalSizeBytes,
      missingSegmentNumbers: [],
      completionStage: "PROBING" as const,
    };
    mocks.selectedSeries.pendingUploads = [completing];
    mocks.getUploadSession.mockResolvedValue(completing);
    mocks.completeUpload.mockResolvedValue({
      uploadSession: { ...completing, status: "COMPLETED", completionStage: null },
      videoId: "video-a",
      jobId: "job-a",
    });
    vi.stubGlobal("EventSource", class {
      onerror: (() => void) | null = null;
      addEventListener() {}
      close() {}
    });

    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "上传面板" }));
    await user.click(screen.getByRole("button", { name: `继续合并 ${file.name}` }));

    await waitFor(() => expect(mocks.completeUpload).toHaveBeenCalledWith(session.uploadSessionId));
    expect(mocks.uploadSegment).not.toHaveBeenCalled();
    expect(mocks.createUploadSession).not.toHaveBeenCalled();
  });

  it("keeps the completion retry available while the server still owns a fresh merge", async () => {
    const completing = {
      ...session,
      status: "COMPLETING" as const,
      uploadedSegmentCount: 3,
      uploadedSizeBytes: session.totalSizeBytes,
      missingSegmentNumbers: [],
      completionStage: "ASSEMBLING" as const,
    };
    mocks.selectedSeries.pendingUploads = [completing];
    mocks.getUploadSession.mockResolvedValue(completing);
    mocks.completeUpload.mockRejectedValue(new ApiClientError(409, {
      message: "服务端仍在合并",
      requestId: "merge-request",
    }));

    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "上传面板" }));
    await user.click(screen.getByRole("button", { name: `继续合并 ${file.name}` }));

    expect(await screen.findByText(/服务端仍在合并.*merge-request/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: `继续合并 ${file.name}` })).toBeEnabled();
    expect(screen.queryByRole("button", { name: `删除 ${file.name}` })).not.toBeInTheDocument();
  });

  it("replays an uncertain creation with the original key before deleting its server session", async () => {
    mocks.createUploadSession.mockRejectedValueOnce(new Error("连接中断"));
    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "开始" }));
    expect(await screen.findByText("连接中断")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: `删除 ${file.name}` }));
    await waitFor(() => expect(screen.queryByText(file.name)).not.toBeInTheDocument());
    expect(mocks.createUploadSession).toHaveBeenCalledTimes(2);
    expect(mocks.createUploadSession.mock.calls[1]).toEqual(mocks.createUploadSession.mock.calls[0]);
    expect(mocks.deleteUploadSession).toHaveBeenCalledWith(session.uploadSessionId);
  });
});
