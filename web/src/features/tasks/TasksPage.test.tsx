import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  VideoParseTaskStatus,
  VideoParseTaskView,
} from "../../api/contracts";
import { TasksPage } from "./TasksPage";

const uploadActions = vi.hoisted(() => ({
  forgetDeletedSession: vi.fn(),
  openDialog: vi.fn(),
  syncRequeuedParse: vi.fn(),
}));

vi.mock("../uploads/UploadProvider", () => ({
  useUploads: () => uploadActions,
}));

function task(
  status: VideoParseTaskStatus,
  id = status.toLowerCase(),
): VideoParseTaskView {
  const failed = status === "PARSING_FAILED" || status === "INDEXING_FAILED";
  return {
    videoParseTaskId: `task-${id}`,
    videoId: `video-${id}`,
    pipelineVersion: "video-parse-v2",
    status,
    parseConfig: {},
    chunkCount: 5,
    version: 1,
    nextExecuteTime: 1,
    attemptCount: status === "PENDING" ? 0 : 1,
    maxAttempts: 3,
    leaseOwner: null,
    leaseVersion: 0,
    leaseExpireTime: null,
    heartbeatTime: null,
    startTime: status === "PENDING" ? null : 1,
    finishTime: status === "COMPLETE" || failed ? 2 : null,
    processingDurationMs: status === "PENDING" ? 0 : 321,
    processingTimingComplete: status === "COMPLETE",
    lastError: failed ? { code: "PARSE_FAILED", message: "解析失败" } : null,
    isTest: false,
    detail: {},
    createTime: 1,
    updateTime: 2,
    tenantId: "local-default",
    seriesId: "series-1",
    seriesName: "MyGO",
    episodeOrder: 1,
    originalFilename: `${id}.mp4`,
    uploadSessionId: `upload-${id}`,
    durationSeconds: 300,
    canRetry: failed,
    canReparse: failed || status === "COMPLETE",
    canDelete: true,
  };
}

function response(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TasksPage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  uploadActions.forgetDeletedSession.mockReset();
  uploadActions.openDialog.mockReset();
  uploadActions.syncRequeuedParse.mockReset();
});

describe("V2 task observation", () => {
  it("reads V2 tasks and filters each persisted status without adapting them to V1 jobs", async () => {
    const tasks = [
      task("PENDING"),
      task("PREPARING"),
      task("PARSING"),
      task("INDEXING"),
      task("PARSING_FAILED"),
      task("INDEXING_FAILED"),
      task("COMPLETE"),
    ];
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response({
      items: tasks,
      total: tasks.length,
      hasMore: false,
      nextCursor: null,
    }));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    const user = userEvent.setup();

    await screen.findByText("pending.mp4");
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "/api/jobs/video-parse/list?tenantId=local-default&limit=100",
    );
    expect(screen.getByRole("region", { name: "任务概况" })).toHaveTextContent("进行中3");
    expect(screen.getByRole("region", { name: "任务概况" })).toHaveTextContent("固定切片35");

    for (const [label, filename] of [
      ["排队中", "pending.mp4"],
      ["准备中", "preparing.mp4"],
      ["解析中", "parsing.mp4"],
      ["发布中", "indexing.mp4"],
      ["解析异常", "parsing_failed.mp4"],
      ["索引异常", "indexing_failed.mp4"],
      ["已完成", "complete.mp4"],
    ] as const) {
      await user.click(screen.getByRole("button", { name: label }));
      expect(screen.getByText(filename)).toBeInTheDocument();
    }
  });

  it("shows only state-backed V2 progress instead of inventing exact chunk progress", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [task("PARSING")],
      total: 1,
      hasMore: false,
      nextCursor: null,
    })));
    const { container } = renderPage();

    expect(await screen.findByRole("heading", { name: "正在解析画面与对白" })).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("固定切片 5 段");
    expect(screen.getByText("321 毫秒（进行中）")).toBeInTheDocument();
    expect(container.querySelector('[class*="pipelineLine"] > span')).toHaveStyle({ width: "0%" });
  });
});

describe("V2 task actions", () => {
  it("labels an unaccepted pending task as an in-place V2 takeover", async () => {
    const pending = {
      ...task("PENDING", "takeover"),
      canReparse: true,
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [pending],
      total: 1,
      hasMore: false,
      nextCursor: null,
    })));
    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", {
      name: `开始 V2 解析 ${pending.originalFilename}`,
    }));
    expect(screen.getByText(/沿用当前任务身份/)).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: `确认开始 V2 解析 ${pending.originalFilename}`,
    })).toBeEnabled();
  });

  it("uses independent server capabilities for reparse and deletion", async () => {
    const childStillRunning = {
      ...task("PARSING_FAILED", "lease-fenced"),
      canRetry: false,
      canReparse: true,
      canDelete: false,
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [childStillRunning],
      total: 1,
      hasMore: false,
      nextCursor: null,
    })));
    renderPage();
    const user = userEvent.setup();

    expect(await screen.findByRole("button", {
      name: `删除任务 ${childStillRunning.originalFilename}`,
    })).toBeDisabled();
    await user.click(screen.getByRole("button", {
      name: `整集重新解析 ${childStillRunning.originalFilename}`,
    }));
    expect(screen.getByRole("button", {
      name: `确认整集重新解析 ${childStillRunning.originalFilename}`,
    })).toBeEnabled();
  });

  it("deletes the owning upload aggregate and removes every card for that session", async () => {
    const failed = { ...task("PARSING_FAILED", "failed"), version: 2 };
    const sibling = {
      ...task("COMPLETE", "sibling"),
      videoId: failed.videoId,
      uploadSessionId: failed.uploadSessionId,
      version: 1,
      canReparse: false,
    };
    let tasks = [failed, sibling];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/video-parse/list") {
        return response({ items: tasks, total: tasks.length, hasMore: false, nextCursor: null });
      }
      if (url.pathname === "/api/uploads/delete") {
        tasks = [];
        return response({ uploadSessionId: failed.uploadSessionId, status: "EXPIRED" });
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    const user = userEvent.setup();

    const deleteLatest = await screen.findByRole("button", {
      name: `删除任务 ${failed.originalFilename}`,
    });
    expect(screen.queryByRole("button", {
      name: `删除任务 ${sibling.originalFilename}`,
    })).not.toBeInTheDocument();

    await user.click(deleteLatest);
    await user.click(screen.getByRole("button", {
      name: `确认删除 ${failed.originalFilename}`,
    }));

    await waitFor(() => expect(screen.queryByText(failed.originalFilename!)).not.toBeInTheDocument());
    expect(screen.queryByText(sibling.originalFilename!)).not.toBeInTheDocument();
    expect(uploadActions.forgetDeletedSession).toHaveBeenCalledWith(failed.uploadSessionId);
  });

  it("retries the same task identity and restores list metadata omitted by the action response", async () => {
    const failed = { ...task("PARSING_FAILED", "failed"), version: 7 };
    const actionResponse: VideoParseTaskView = {
      ...failed,
      status: "PENDING",
      version: 8,
      attemptCount: 3,
      maxAttempts: 4,
      lastError: null,
      tenantId: null,
      seriesId: null,
      seriesName: null,
      episodeOrder: null,
      originalFilename: null,
      uploadSessionId: null,
      durationSeconds: null,
      canRetry: false,
      canReparse: false,
      canDelete: false,
    };
    let serverTask = failed;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/video-parse/list") {
        return response({ items: [serverTask], total: 1, hasMore: false, nextCursor: null });
      }
      if (url.pathname === "/api/uploads/retry-parse") {
        serverTask = {
          ...failed,
          ...actionResponse,
          tenantId: failed.tenantId,
          seriesId: failed.seriesId,
          seriesName: failed.seriesName,
          episodeOrder: failed.episodeOrder,
          originalFilename: failed.originalFilename,
          uploadSessionId: failed.uploadSessionId,
          durationSeconds: failed.durationSeconds,
        };
        return response(actionResponse);
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", {
      name: `重试解析 ${failed.originalFilename}`,
    }));

    await waitFor(() => expect(screen.queryByRole("button", {
      name: `重试解析 ${failed.originalFilename}`,
    })).not.toBeInTheDocument());
    expect(screen.getByText(failed.originalFilename!)).toBeInTheDocument();
    expect(screen.getByText("3 / 4")).toBeInTheDocument();
    expect(uploadActions.syncRequeuedParse).toHaveBeenCalledWith(
      expect.objectContaining({
        videoParseTaskId: failed.videoParseTaskId,
        uploadSessionId: failed.uploadSessionId,
        originalFilename: failed.originalFilename,
        status: "PENDING",
      }),
    );
  });

  it("reparse replaces the old card identity while retaining its upload metadata", async () => {
    const completed = { ...task("COMPLETE", "complete"), version: 11 };
    const actionResponse: VideoParseTaskView = {
      ...completed,
      videoParseTaskId: "task-reparsed",
      status: "PENDING",
      version: 12,
      attemptCount: 0,
      processingDurationMs: 0,
      processingTimingComplete: true,
      finishTime: null,
      tenantId: null,
      seriesId: null,
      seriesName: null,
      episodeOrder: null,
      originalFilename: null,
      uploadSessionId: null,
      durationSeconds: null,
      canRetry: false,
      canReparse: false,
      canDelete: false,
    };
    let serverTask = completed;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/video-parse/list") {
        return response({ items: [serverTask], total: 1, hasMore: false, nextCursor: null });
      }
      if (url.pathname === "/api/uploads/reparse") {
        serverTask = {
          ...completed,
          ...actionResponse,
          tenantId: completed.tenantId,
          seriesId: completed.seriesId,
          seriesName: completed.seriesName,
          episodeOrder: completed.episodeOrder,
          originalFilename: completed.originalFilename,
          uploadSessionId: completed.uploadSessionId,
          durationSeconds: completed.durationSeconds,
        };
        return response(actionResponse);
      }
      throw new Error(`Unexpected request: ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", {
      name: `整集重新解析 ${completed.originalFilename}`,
    }));
    expect(screen.getByText(/建立新的 V2 任务身份/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", {
      name: `确认整集重新解析 ${completed.originalFilename}`,
    }));

    await waitFor(() => expect(screen.queryByRole("button", {
      name: `整集重新解析 ${completed.originalFilename}`,
    })).not.toBeInTheDocument());
    expect(screen.getByText(completed.originalFilename!)).toBeInTheDocument();
    expect(uploadActions.syncRequeuedParse).toHaveBeenCalledWith(
      expect.objectContaining({
        videoParseTaskId: "task-reparsed",
        uploadSessionId: completed.uploadSessionId,
        originalFilename: completed.originalFilename,
        status: "PENDING",
      }),
    );
  });
});
