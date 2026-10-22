import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError } from "../../api/client";
import type {
  ParseConfig,
  UploadSessionView,
  VideoParseTaskView,
} from "../../api/contracts";
import { UploadDialog } from "./UploadDialog";
import { UploadProvider, useUploads } from "./UploadProvider";

const mocks = vi.hoisted(() => ({
  displayMode: "release" as "release" | "debug",
  selectedSeries: { seriesId: "series-a", name: "测试系列", videos: [], pendingUploads: [] as UploadSessionView[] },
  refreshSeries: vi.fn(),
  createUploadSession: vi.fn(),
  getParseConfig: vi.fn(),
  getUploadSession: vi.fn(),
  getVideoParseTask: vi.fn(),
  deleteUploadSession: vi.fn(),
  completeUpload: vi.fn(),
  uploadSegment: vi.fn(),
}));
vi.mock("../../app/workspace", () => ({ useWorkspace: () => ({ ...mocks, mode: "live" }) }));
vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  createUploadSession: mocks.createUploadSession,
  getParseConfig: mocks.getParseConfig,
  getUploadSession: mocks.getUploadSession,
  getVideoParseTask: mocks.getVideoParseTask,
  deleteUploadSession: mocks.deleteUploadSession,
  completeUpload: mocks.completeUpload,
  uploadSegment: mocks.uploadSegment,
}));

const file = new File(["abcdefghij"], "episode.mp4", { type: "video/mp4" });
const parseConfig: ParseConfig = {
  version: 2,
  window: { chunkDurationSeconds: 60, contextOverlapSeconds: 3, analysisMaxHeight: 720, canonicalMaxHeight: 720 },
  asr: { mode: "lite", model: "lite", fps: 1, maxOutputTokens: null, concurrency: 4 },
  vision: { model: "pro", inputMode: "video", fps: 1, maxFramesPerWindow: null, maxOutputTokens: null },
  context: { maxEvents: 8, maxChars: 2000, backgroundTextEnabled: true, backgroundTextMaxChars: null,
    backgroundImagesEnabled: true, maxBackgroundImages: null, backgroundImageMaxEdge: 1024 },
  fallback: { enabled: true, model: "flash", frameFps: 0.5, groupDurationSeconds: 30, maxFramesPerGroup: 15,
    maxHeight: 720, reasoningEffort: "none", maxOutputTokens: 8192, requestTimeoutSeconds: 1200 },
  embedding: { model: "embedding", dimensions: 1024, videoFps: 1 },
  execution: { requestTimeoutSeconds: 600, maxAttempts: 3 },
};
const session: UploadSessionView = {
  uploadSessionId: "session-a", tenantId: "local-default", seriesId: "series-a",
  episodeOrder: 1, uploadSequence: 1, originalFilename: file.name, mediaType: file.type,
  totalSizeBytes: 10, contentSha256: null, preferredSegmentSizeBytes: 4, totalSegments: 3,
  status: "UPLOADING", uploadedSegmentCount: 0, uploadedSizeBytes: 0,
  missingSegmentNumbers: [0, 1, 2], completionStage: null, completionProgress: null,
  estimatedRemainingMs: null, expiresAt: 100, videoId: null, jobId: null, lastError: null,
  version: 1, createdAt: 1, updatedAt: 1,
};

const videoParseTask: VideoParseTaskView = {
  videoParseTaskId: "task-a",
  videoId: "video-a",
  pipelineVersion: "video-parse-v2",
  status: "PARSING",
  parseConfig: {},
  chunkCount: 2,
  version: 1,
  nextExecuteTime: 1,
  attemptCount: 1,
  maxAttempts: 3,
  leaseOwner: "worker-a",
  leaseVersion: 1,
  leaseExpireTime: 3,
  heartbeatTime: 2,
  startTime: 1,
  finishTime: null,
  processingDurationMs: 100,
  processingTimingComplete: false,
  lastError: null,
  isTest: false,
  detail: {},
  createTime: 1,
  updateTime: 2,
  tenantId: "local-default",
  seriesId: "series-a",
  seriesName: "测试系列",
  episodeOrder: 1,
  originalFilename: file.name,
  uploadSessionId: session.uploadSessionId,
  durationSeconds: 10,
  canRetry: false,
  canReparse: false,
  canDelete: false,
};

function Controls() {
  const {
    enqueue,
    forgetDeletedSession,
    openDialog,
    syncRequeuedParse,
  } = useUploads();
  return <>
    <button onClick={() => { enqueue([{ file, episodeOrder: 99 }]); openDialog(); }}>开始</button>
    <button onClick={openDialog}>上传面板</button>
    <button onClick={() => forgetDeletedSession(session.uploadSessionId)}>同步外部删除</button>
    <button onClick={() => syncRequeuedParse({ ...videoParseTask, attemptCount: 3, maxAttempts: 4 })}>同步解析重试</button>
    <UploadDialog />
  </>;
}

beforeEach(() => {
  vi.unstubAllGlobals();
  mocks.displayMode = "release";
  mocks.selectedSeries.pendingUploads = [];
  mocks.refreshSeries.mockReset().mockResolvedValue(undefined);
  mocks.createUploadSession.mockReset().mockResolvedValue(session);
  mocks.getParseConfig.mockReset().mockResolvedValue(parseConfig);
  mocks.getUploadSession.mockReset().mockResolvedValue(session);
  mocks.getVideoParseTask.mockReset().mockResolvedValue(videoParseTask);
  mocks.deleteUploadSession.mockReset().mockResolvedValue({ ...session, status: "EXPIRED" });
  mocks.completeUpload.mockReset();
  mocks.uploadSegment.mockReset().mockImplementation(({ signal }: { signal: AbortSignal }) =>
    new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }),
  );
});

it("sends the draft profile and displays the canonical V2 upload configuration", async () => {
  mocks.displayMode = "debug";
  mocks.createUploadSession.mockResolvedValue({ ...session, parseConfig: {
    visual_input: { mode: "ordered_frames", max_height: 360, fps: 0.25, max_frames_per_chunk: 8 },
  } });
  const { container } = render(<UploadProvider><Controls /></UploadProvider>);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "上传面板" }));
  await waitFor(() => expect(mocks.getParseConfig).toHaveBeenCalled());
  const input = container.querySelector<HTMLInputElement>('input[type="file"][multiple]');
  expect(input).not.toBeNull();
  await user.upload(input!, file);
  await user.click(screen.getByRole("radio", { name: "自定义" }));
  const height = screen.getByRole("spinbutton", { name: "解析输入高度（像素）" });
  await user.clear(height);
  await user.type(height, "480");
  const fps = screen.getByRole("spinbutton", { name: "取帧 FPS" });
  await user.clear(fps);
  await user.type(fps, "0.5");
  await user.click(screen.getByRole("button", { name: "开始并行上传" }));
  await waitFor(() => expect(mocks.createUploadSession).toHaveBeenCalled());
  expect(mocks.createUploadSession.mock.calls[0]?.[2].parseConfig).toMatchObject({
    window: { analysisMaxHeight: 480, canonicalMaxHeight: 720 },
    vision: { fps: 0.5 },
  });
  expect(await screen.findByText(/解析配置：360p.*0.25 FPS.*每段最多 8 帧/)).toBeInTheDocument();
});

it("uploads with server defaults in release without loading debug configuration", async () => {
  const { container } = render(<UploadProvider><Controls /></UploadProvider>);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "上传面板" }));
  expect(screen.queryByRole("region", { name: "解析配置" })).not.toBeInTheDocument();
  expect(mocks.getParseConfig).not.toHaveBeenCalled();
  await user.upload(container.querySelector<HTMLInputElement>('input[type="file"][multiple]')!, file);
  await user.click(screen.getByRole("button", { name: "开始并行上传" }));
  await waitFor(() => expect(mocks.createUploadSession).toHaveBeenCalledOnce());
  expect(mocks.createUploadSession.mock.calls[0]?.[2]).toMatchObject({ originalFilename: file.name });
  expect(mocks.createUploadSession.mock.calls[0]?.[2].parseConfig).toBeUndefined();
});

it.each([false, true])("uses server defaults after switching a custom upload to release (locked: %s)", async (locked) => {
  mocks.displayMode = "debug";
  const view = render(<UploadProvider><Controls /></UploadProvider>);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "上传面板" }));
  await waitFor(() => expect(mocks.getParseConfig).toHaveBeenCalledOnce());
  await user.upload(view.container.querySelector<HTMLInputElement>('input[type="file"][multiple]')!, file);
  await user.click(screen.getByRole("radio", { name: "自定义" }));
  const fps = screen.getByRole("spinbutton", { name: "取帧 FPS" });
  await user.clear(fps);
  await user.type(fps, "0.5");
  if (locked) {
    await user.click(screen.getByRole("button", { name: "锁定本集配置" }));
    expect(screen.getByRole("button", { name: "已锁定本集配置" })).toBeInTheDocument();
  }

  mocks.displayMode = "release";
  view.rerender(<UploadProvider><Controls /></UploadProvider>);
  expect(screen.queryByRole("region", { name: "解析配置" })).not.toBeInTheDocument();
  expect(screen.queryByText("已锁定本集配置")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "开始并行上传" }));
  await waitFor(() => expect(mocks.createUploadSession).toHaveBeenCalledOnce());
  expect(mocks.createUploadSession.mock.calls[0]?.[2].parseConfig).toBeUndefined();
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
    expect(await screen.findByText("删除失败：暂时不可用")).toBeInTheDocument();
    expect(screen.queryByText(/request-a/)).not.toBeInTheDocument();
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

  it("reattaches a recovered in-memory task when its parse task is manually retried", async () => {
    mocks.displayMode = "debug";
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

    await waitFor(() => {
      const task = screen.getByText(file.name).closest("article");
      expect(task).toHaveTextContent("解析画面与对白");
      expect(task).toHaveTextContent("100%");
      expect(task).not.toHaveTextContent("本阶段");
      expect(task).toHaveTextContent("3/4 次尝试");
      expect(task).toHaveTextContent("2 个固定切片");
    });
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
      videoParseTaskId: "task-a",
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

  it("observes a V2 task identity when completion has no legacy job", async () => {
    mocks.displayMode = "debug";
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
      uploadSession: {
        ...completing,
        status: "COMPLETED",
        completionStage: null,
        initialVideoParseTaskId: "task-a",
      },
      videoId: "video-a",
      jobId: null,
      videoParseTaskId: "task-a",
    });
    let source!: EventTarget & { close: ReturnType<typeof vi.fn>; url: string };
    vi.stubGlobal("EventSource", class extends EventTarget {
      onerror: (() => void) | null = null;
      close = vi.fn();
      url: string;
      constructor(url: string) {
        super();
        this.url = url;
        source = this;
      }
    });

    render(<UploadProvider><Controls /></UploadProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "上传面板" }));
    await user.click(screen.getByRole("button", { name: `继续合并 ${file.name}` }));

    await waitFor(() => expect(source.url).toBe(
      "/api/jobs/video-parse/events?videoParseTaskId=task-a&tenantId=local-default",
    ));
    act(() => source.dispatchEvent(new MessageEvent("video-parse-task", {
      data: JSON.stringify(videoParseTask),
    })));
    await waitFor(() => expect(screen.getByText(file.name).closest("article")).toHaveTextContent(
      "解析画面与对白 · 2 个固定切片",
    ));

    act(() => source.dispatchEvent(new MessageEvent("video-parse-task", {
      data: JSON.stringify({
        ...videoParseTask,
        status: "COMPLETE",
        finishTime: 3,
        processingTimingComplete: true,
        updateTime: 3,
      }),
    })));
    await waitFor(() => expect(screen.getByText(file.name).closest("article")).toHaveTextContent("已可检索"));
    expect(source.close).toHaveBeenCalledOnce();
    expect(mocks.refreshSeries).toHaveBeenCalled();
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

    expect(await screen.findByText("服务端仍在合并")).toBeInTheDocument();
    expect(screen.queryByText(/merge-request/)).not.toBeInTheDocument();
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
