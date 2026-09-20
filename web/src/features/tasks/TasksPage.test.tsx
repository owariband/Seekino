import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ParseJobView } from "../../api/contracts";
import { TasksPage } from "./TasksPage";

const uploadActions = vi.hoisted(() => ({
  forgetDeletedSession: vi.fn(),
  openDialog: vi.fn(),
  syncRequeuedParse: vi.fn(),
}));

vi.mock("../uploads/UploadProvider", () => ({
  useUploads: () => uploadActions,
}));

vi.mock("./ParseUsageDetails", () => ({
  ParseUsageDetails: () => null,
}));

function job(status: ParseJobView["status"], id = status.toLowerCase()): ParseJobView {
  return {
    jobId: `job-${id}`,
    uploadSessionId: `upload-${id}`,
    workspaceId: "local-default",
    seriesId: "series-1",
    videoId: `video-${id}`,
    seriesName: "MyGO",
    episodeOrder: 1,
    originalFilename: `${id}.mp4`,
    pipelineVersion: "m1",
    retryOfJobId: null,
    status,
    stage: status === "FAILED" ? "PARSING" : "PERSISTING",
    progress: status === "FAILED" ? 18 : 70,
    overallProgress: status === "FAILED" ? 18 : 70,
    stageProgress: status === "FAILED" ? 18 : null,
    completedDurationMs: 120_000,
    completedThroughMs: 120_000,
    videoDurationMs: 300_000,
    totalDurationMs: 300_000,
    processingDurationMs: 321,
    processingTimingComplete: false,
    completedChunkCount: 2,
    totalChunkCount: 5,
    currentChunk: null,
    attemptCount: 1,
    maxAttempts: 3,
    availableAt: null,
    startedAt: 1,
    completedAt: status === "FAILED" ? 2 : null,
    estimatedRemainingMs: null,
    canRetry: status === "FAILED",
    canReparse: status === "FAILED" || status === "SUCCEEDED",
    canCancel: false,
    lastError: status === "FAILED" ? { code: "PARSE_FAILED", message: "解析失败" } : null,
    version: 1,
    checkpoints: [],
    createdAt: 1,
    updatedAt: 2,
  };
}

function response(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><TasksPage /></QueryClientProvider>);
}

afterEach(() => {
  vi.unstubAllGlobals();
  uploadActions.forgetDeletedSession.mockReset();
  uploadActions.openDialog.mockReset();
  uploadActions.syncRequeuedParse.mockReset();
});

describe("task status filters", () => {
  it("keeps queued, running, retrying, failed, and completed tasks in separate filters", async () => {
    const jobs = [
      job("QUEUED"),
      job("RUNNING"),
      job("RETRY_WAIT"),
      job("FAILED"),
      job("SUCCEEDED"),
    ];
    vi.stubGlobal("fetch", vi.fn(async () => response({ items: jobs, hasMore: false, nextCursor: null })));
    renderPage();
    const user = userEvent.setup();

    await screen.findByText("queued.mp4");
    expect(screen.getByRole("region", { name: "任务概况" })).toHaveTextContent("进行中1");
    for (const [label, filename] of [
      ["排队中", "queued.mp4"],
      ["进行中", "running.mp4"],
      ["等待重试", "retry_wait.mp4"],
      ["异常", "failed.mp4"],
      ["已完成", "succeeded.mp4"],
    ] as const) {
      await user.click(screen.getByRole("button", { name: label }));
      expect(screen.getByText(filename)).toBeInTheDocument();
      for (const other of jobs) {
        if (other.originalFilename !== filename) {
          expect(screen.queryByText(other.originalFilename)).not.toBeInTheDocument();
        }
      }
    }
  });
});

describe("processing timing", () => {
  it("shows live chunk timing without hiding unstarted or incomplete timing states", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [
        { ...job("RUNNING"), processingDurationMs: 321, processingTimingComplete: false },
        { ...job("QUEUED"), processingDurationMs: 0, processingTimingComplete: true, attemptCount: 0 },
        { ...job("FAILED"), processingDurationMs: 654, processingTimingComplete: false },
      ],
      hasMore: false,
      nextCursor: null,
    })));
    renderPage();

    expect(await screen.findAllByText("累计解析耗时")).toHaveLength(2);
    expect(await screen.findByText("321 毫秒（进行中）")).toBeInTheDocument();
    expect(await screen.findByText("未开始")).toBeInTheDocument();
    expect(await screen.findByText("至少 654 毫秒（不完整）")).toBeInTheDocument();
  });
});

describe("task deletion", () => {
  it("deletes the UploadSessionTask from a failed parse card and disables deletion while running", async () => {
    const failed = job("FAILED");
    const sibling = {
      ...job("SUCCEEDED", "sibling"),
      uploadSessionId: failed.uploadSessionId,
    };
    const running = job("RUNNING");
    let jobs = [failed, sibling, running];
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/list") return response({ items: jobs, hasMore: false, nextCursor: null });
      if (url.pathname === "/api/uploads/delete") {
        await pending;
        jobs = jobs.filter((item) => item.uploadSessionId !== failed.uploadSessionId);
        return response({ uploadSessionId: failed.uploadSessionId, status: "EXPIRED" });
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    const user = userEvent.setup();

    const failedButton = await screen.findByRole("button", { name: `删除任务 ${failed.originalFilename}` });
    expect(screen.getAllByText("解析失败")).not.toHaveLength(0);
    expect(screen.getAllByText("文件已上传")).toHaveLength(3);
    expect(screen.getByRole("button", { name: `删除任务 ${running.originalFilename}` })).toBeDisabled();
    await user.click(failedButton);
    expect(screen.getByText("确认删除这个视频任务？")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/uploads/delete"))).toBe(false);
    await user.click(screen.getByRole("button", { name: `确认删除 ${failed.originalFilename}` }));
    expect(failedButton).toBeDisabled();
    expect(failedButton).toHaveTextContent("正在删除");
    expect(screen.getByRole("button", { name: `删除任务 ${sibling.originalFilename}` })).toBeDisabled();
    release();

    await waitFor(() => expect(screen.queryByText(failed.originalFilename)).not.toBeInTheDocument());
    expect(screen.queryByText(sibling.originalFilename)).not.toBeInTheDocument();
    expect(uploadActions.forgetDeletedSession).toHaveBeenCalledWith(failed.uploadSessionId);
    const deletion = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/uploads/delete"));
    expect(String(deletion?.[0])).toBe("/api/uploads/delete?uploadSessionId=upload-failed&workspaceId=local-default");
    expect(deletion?.[1]?.method).toBe("POST");
  });

  it("keeps the task and exposes the request identifier when deletion fails", async () => {
    const failed = job("FAILED");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/list") return response({ items: [failed], hasMore: false, nextCursor: null });
      if (url.pathname === "/api/uploads/delete") {
        return response({ message: "任务正在执行", requestId: "delete-request" }, 409);
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    }));
    renderPage();
    const user = userEvent.setup();

    const button = await screen.findByRole("button", { name: `删除任务 ${failed.originalFilename}` });
    await user.click(button);
    await user.click(screen.getByRole("button", { name: `确认删除 ${failed.originalFilename}` }));

    expect(await screen.findByRole("alert")).toHaveTextContent("删除失败：任务正在执行（请求编号：delete-request）");
    expect(button).toBeEnabled();
    expect(screen.getByText(failed.originalFilename)).toBeInTheDocument();
  });
});

describe("manual parse retry", () => {
  it("requeues a failed UploadSessionTask once and disables delete while submitting", async () => {
    const failed = { ...job("FAILED"), attemptCount: 3, maxAttempts: 3, version: 7 };
    const retried: ParseJobView = {
      ...failed,
      status: "QUEUED",
      attemptCount: 3,
      maxAttempts: 4,
      version: 8,
      canRetry: false,
      canReparse: false,
      completedAt: null,
      lastError: null,
    };
    let jobs = [failed];
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/list") return response({ items: jobs, hasMore: false, nextCursor: null });
      if (url.pathname === "/api/uploads/retry-parse") {
        await pending;
        jobs = [retried];
        return response(retried);
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    const user = userEvent.setup();

    const retryButton = await screen.findByRole("button", { name: `重试解析 ${failed.originalFilename}` });
    const deleteButton = screen.getByRole("button", { name: `删除任务 ${failed.originalFilename}` });
    await user.click(retryButton);

    expect(retryButton).toBeDisabled();
    expect(retryButton).toHaveTextContent("正在重试");
    expect(deleteButton).toBeDisabled();
    release();

    await waitFor(() => expect(screen.queryByRole("button", { name: `重试解析 ${failed.originalFilename}` })).not.toBeInTheDocument());
    expect(uploadActions.syncRequeuedParse).toHaveBeenCalledWith(retried);
    expect(screen.getByText("排队中", { selector: '[data-status="QUEUED"]' })).toBeInTheDocument();
    expect(screen.getByText("3 / 4")).toBeInTheDocument();
    const retry = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/uploads/retry-parse"));
    expect(String(retry?.[0])).toBe("/api/uploads/retry-parse?uploadSessionId=upload-failed&workspaceId=local-default&expectedJobVersion=7");
  });

  it("keeps a failed task retryable and shows the request identifier on conflict", async () => {
    const failed = { ...job("FAILED"), version: 9 };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/list") return response({ items: [failed], hasMore: false, nextCursor: null });
      if (url.pathname === "/api/uploads/retry-parse") {
        return response({ message: "任务状态已变化，请刷新", requestId: "retry-request" }, 409);
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    }));
    renderPage();
    const user = userEvent.setup();

    const button = await screen.findByRole("button", { name: `重试解析 ${failed.originalFilename}` });
    await user.click(button);

    expect(await screen.findByRole("alert")).toHaveTextContent("重试失败：任务状态已变化，请刷新（请求编号：retry-request）");
    expect(button).toBeEnabled();
  });
});

describe("full episode reparse", () => {
  it("confirms the destructive checkpoint reset and requeues the same job", async () => {
    const completed = {
      ...job("SUCCEEDED"),
      stage: "COMPLETED",
      progress: 100,
      overallProgress: 100,
      version: 11,
    };
    const reparsed: ParseJobView = {
      ...completed,
      status: "QUEUED",
      stage: "QUEUED",
      progress: 0,
      overallProgress: null,
      completedDurationMs: null,
      completedThroughMs: null,
      completedChunkCount: null,
      startedAt: null,
      completedAt: null,
      version: 12,
      canReparse: false,
      checkpoints: [],
    };
    let jobs: ParseJobView[] = [completed];
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/list") return response({ items: jobs, hasMore: false, nextCursor: null });
      if (url.pathname === "/api/uploads/reparse") {
        await pending;
        jobs = [reparsed];
        return response(reparsed);
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    const user = userEvent.setup();

    const reparseButton = await screen.findByRole("button", {
      name: `整集重新解析 ${completed.originalFilename}`,
    });
    await user.click(reparseButton);
    expect(screen.getByText("从头重新解析这一整集？")).toBeInTheDocument();
    expect(screen.getByText(/当前可检索结果会保留到新结果导入成功/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/uploads/reparse"))).toBe(false);

    await user.click(screen.getByRole("button", {
      name: `确认整集重新解析 ${completed.originalFilename}`,
    }));
    expect(reparseButton).toBeDisabled();
    expect(reparseButton).toHaveTextContent("正在重新解析");
    expect(screen.getByRole("button", {
      name: `删除任务 ${completed.originalFilename}`,
    })).toBeDisabled();
    release();

    await waitFor(() => expect(screen.queryByRole("button", {
      name: `整集重新解析 ${completed.originalFilename}`,
    })).not.toBeInTheDocument());
    expect(uploadActions.syncRequeuedParse).toHaveBeenCalledWith(reparsed);
    const call = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/uploads/reparse"));
    expect(String(call?.[0])).toBe("/api/uploads/reparse?uploadSessionId=upload-succeeded&workspaceId=local-default&expectedJobVersion=11");
  });
});

describe("segment progress presentation", () => {
  it("shows one build progress and groups internal processing stages in the pipeline", async () => {
    const running: ParseJobView = {
      ...job("RUNNING"),
      stage: "PARSING",
      progress: 99,
      overallProgress: 37,
      stageProgress: 85,
      completedDurationMs: 120_000,
      completedThroughMs: 120_000,
      videoDurationMs: 140_000,
      totalDurationMs: 140_000,
      completedChunkCount: 2,
      totalChunkCount: 3,
      currentChunk: { sequenceNo: 2, startMs: 120_000, endMs: 140_000 },
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({ items: [running], hasMore: false, nextCursor: null })));

    const { container } = renderPage();

    expect(await screen.findByText("37%")).toBeInTheDocument();
    expect(screen.getByText("任务进度")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "正在解析画面与对白" })).toBeInTheDocument();
    expect(screen.queryByText("85%")).not.toBeInTheDocument();
    expect(screen.queryByText("99%")).not.toBeInTheDocument();
    expect(screen.queryByText(/本阶段/)).not.toBeInTheDocument();
    expect(screen.getByText("文件已上传")).toBeInTheDocument();
    expect(screen.getByText("解析画面与对白")).toBeInTheDocument();
    expect(screen.getByText("发布检索结果")).toBeInTheDocument();
    expect(screen.getByText("可检索")).toBeInTheDocument();
    const facts = screen.getByLabelText("解析任务进度");
    expect(facts).toHaveTextContent("连续解析到 02:00 / 02:20");
    expect(facts).toHaveTextContent("已解析 2 / 3 段");
    expect(facts).toHaveTextContent("当前解析 02:00–02:20");
    expect(screen.getByText("每 5 秒自动同步 · 手动刷新可立即校准")).toBeInTheDocument();
    expect(container.querySelector('[class*="pipelineLine"] > span')).toHaveStyle({ width: "0%" });
  });

  it("does not substitute the legacy progress when exact progress is unavailable", async () => {
    const preparing: ParseJobView = {
      ...job("RUNNING"),
      stage: "PREPARING",
      progress: 92,
      overallProgress: null,
      stageProgress: null,
      completedDurationMs: null,
      completedThroughMs: null,
      completedChunkCount: null,
      currentChunk: null,
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({ items: [preparing], hasMore: false, nextCursor: null })));

    renderPage();

    expect(await screen.findByText("等待检查点")).toBeInTheDocument();
    expect(screen.queryByText("92%")).not.toBeInTheDocument();
    expect(screen.getAllByText("—")).toHaveLength(1);
  });

  it("does not show a current segment after parsing has completed", async () => {
    const completed: ParseJobView = {
      ...job("SUCCEEDED"),
      stage: "COMPLETED",
      progress: 100,
      overallProgress: 100,
      stageProgress: 100,
      completedDurationMs: 140_000,
      completedThroughMs: 140_000,
      videoDurationMs: 140_000,
      totalDurationMs: 140_000,
      completedChunkCount: 3,
      totalChunkCount: 3,
      currentChunk: null,
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({ items: [completed], hasMore: false, nextCursor: null })));

    renderPage();

    const facts = await screen.findByLabelText("解析任务进度");
    expect(facts).toHaveTextContent("连续解析到 02:20 / 02:20");
    expect(facts).toHaveTextContent("已解析 3 / 3 段");
    expect(facts).not.toHaveTextContent("当前解析");
  });
});
