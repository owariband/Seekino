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
const workspace = vi.hoisted(() => ({ displayMode: "debug" as "release" | "debug" }));

vi.mock("../../app/workspace", () => ({
  useWorkspace: () => workspace,
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
  workspace.displayMode = "debug";
  vi.unstubAllGlobals();
  uploadActions.forgetDeletedSession.mockReset();
  uploadActions.openDialog.mockReset();
  uploadActions.syncRequeuedParse.mockReset();
});

describe("V2 task observation", () => {
  it("hides internal parsing statistics in release while preserving progress, errors and actions", async () => {
    workspace.displayMode = "release";
    const failed: VideoParseTaskView = {
      ...task("PARSING_FAILED"),
      chunkCount: 24,
      detail: {
        progress: {
          completedChunks: 24, totalChunks: 24, completedRefines: 8, totalRefines: 22,
          completedUnits: 32, totalUnits: 70, percent: 95 * 32 / 46,
          topologyReady: true, indexedChunks: 0,
        },
      },
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response({
      items: [failed], total: 1, hasMore: false, nextCursor: null,
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { container } = renderPage();

    expect(await screen.findByLabelText("解析进度")).toBeInTheDocument();
    expect(screen.getByLabelText("解析进度")).toHaveTextContent("66.1%");
    expect(screen.queryByLabelText("解析任务进度")).not.toBeInTheDocument();
    expect(screen.queryByText("模型消耗")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/usage/overview"))).toBe(false);
    expect(container).not.toHaveTextContent(/固定切片|理解|Refine|索引\s*0\s*\/\s*22|完成单元|有效解析时长/);
    expect(screen.getByText("解析失败")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: `重试解析 ${failed.originalFilename}` })).toBeEnabled();
    expect(screen.getByRole("button", { name: `整集重新解析 ${failed.originalFilename}` })).toBeEnabled();
    expect(screen.getByRole("button", { name: `删除任务 ${failed.originalFilename}` })).toBeEnabled();
    expect(screen.getByRole("button", { name: "管理上传" })).toBeEnabled();
  });

  it("loads PPE model usage for the actual V2 task only after expanding its panel", async () => {
    const parsing = task("PARSING", "ppe");
    const cost = { currency: "CNY", estimatedAmount: "0.12", knownAmount: "0.12",
      pricedCallCount: 1, unpricedCallCount: 0, excludedCallCount: 0 };
    const totals = { callCount: 1, inputTokens: 100, outputTokens: 20, cachedInputTokens: 10,
      cacheWriteInputTokens: 0, reasoningTokens: 0, totalTokens: 120, knownTokens: 120, missingUsageCount: 0, cost };
    const usage = { source: "local", status: "available", message: null, fromStartTime: 1, toStartTime: 2,
      fetchedAt: 2, truncated: false, totals,
      models: [{ ...totals, provider: "ark", src: "endpoint-ppe", model: "api-model", category: "parse", operation: "understand" }], runs: [] };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => response(String(input).includes("/api/usage/overview")
      ? usage : { items: [parsing], total: 1, hasMore: false, nextCursor: null }));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await screen.findByText(parsing.originalFilename!);
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/usage/overview"))).toBe(false);

    await userEvent.setup().click(screen.getByText("模型消耗"));
    expect(await screen.findByText("endpoint-ppe")).toBeInTheDocument();
    expect(screen.getByText("api-model")).toBeInTheDocument();
    expect(screen.getByText("ark")).toBeInTheDocument();
    expect(screen.getAllByText("¥0.12")).toHaveLength(2);
    expect(screen.getAllByText("120")).toHaveLength(2);
    const usageCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/usage/overview"));
    expect(usageCalls).toHaveLength(1);
    expect(String(usageCalls[0]![0])).toBe(`/api/usage/overview?tenantId=local-default&runId=${parsing.videoParseTaskId}`);
  });

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
    const { container } = renderPage();
    const user = userEvent.setup();

    await screen.findByText("pending.mp4");
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "/api/jobs/video-parse/list?tenantId=local-default&limit=100",
    );
    expect(screen.getByRole("region", { name: "任务概况" })).toHaveTextContent("进行中3");
    expect(screen.getByRole("region", { name: "任务概况" })).toHaveTextContent("已完成1");
    expect(screen.getByRole("region", { name: "任务概况" })).not.toHaveTextContent("固定切片");
    expect(container).not.toHaveTextContent(/任务身份|租约版本|V2 任务状态|持久状态|尝试次数|流水线/);

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

  it("uses task status for failure panels even when error details are stale or missing", async () => {
    const statuses: VideoParseTaskStatus[] = ["PENDING", "PREPARING", "PARSING", "INDEXING", "COMPLETE"];
    const tasks = [
      ...statuses.map((status) => ({
        ...task(status),
        lastError: { code: "PROCESSING_FAILED", message: "历史租约过期错误" },
      })),
      { ...task("PARSING_FAILED"), lastError: null },
      { ...task("INDEXING_FAILED"), lastError: null },
    ];
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: tasks, total: tasks.length, hasMore: false, nextCursor: null,
    })));
    renderPage();

    await screen.findByText("parsing.mp4");
    expect(screen.queryAllByText("历史租约过期错误")).toHaveLength(0);
    expect(screen.getAllByText("失败原因")).toHaveLength(2);
    expect(screen.getByText("视频解析失败，请重试")).toBeInTheDocument();
    expect(screen.getByText("检索索引发布失败，请重试")).toBeInTheDocument();
  });

  it("keeps the persisted-status fallback for responses without progress statistics", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [task("PARSING")],
      total: 1,
      hasMore: false,
      nextCursor: null,
    })));
    const { container } = renderPage();

    expect(await screen.findByText("正在解析画面与对白")).toBeInTheDocument();
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("固定切片 5 段");
    expect(screen.getByText("有效解析时长").parentElement).toHaveTextContent("00:00");
    expect(container.querySelector('[class*="pipelineLine"] > span')).toHaveStyle({ width: "0%" });
  });

  it("shows weighted persisted progress and gains one parsing unit on the next snapshot", async () => {
    let serverTask: VideoParseTaskView = {
      ...task("PARSING_FAILED"),
      chunkCount: 24,
      detail: {
        progress: {
          completedChunks: 24, totalChunks: 24,
          completedRefines: 8, totalRefines: 22,
          completedUnits: 32, totalUnits: 70,
          percent: 95 * 32 / 46, topologyReady: true, indexedChunks: 0,
        },
      },
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [serverTask], total: 1, hasMore: false, nextCursor: null,
    })));
    const { container } = renderPage();
    const user = userEvent.setup();

    await screen.findByLabelText("解析进度");
    expect(screen.getByLabelText("解析进度")).toHaveTextContent("66.1%");
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("理解 24 / 24");
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("Refine 8 / 22");
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("索引 0 / 24");
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("完成单元 32 / 70");
    expect(container.querySelector('[class*="pipelineLine"] > span')).toHaveStyle({ width: `${95 * 32 / 46}%` });

    serverTask = {
      ...serverTask,
      detail: {
        progress: {
          ...serverTask.detail.progress!,
          completedRefines: 9, completedUnits: 33, percent: 95 * 33 / 46,
        },
      },
    };
    await user.click(screen.getByRole("button", { name: "同步状态" }));
    await waitFor(() => expect(screen.getByLabelText("解析进度")).toHaveTextContent("68.2%"));
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("Refine 9 / 22");
  });

  it("shows preparation instead of a percentage before the topology is ready", async () => {
    const preparing: VideoParseTaskView = {
      ...task("PREPARING"),
      detail: {
        progress: {
          completedChunks: 0, totalChunks: 0,
          completedRefines: 0, totalRefines: 0,
          completedUnits: 0, totalUnits: 0,
          percent: null, topologyReady: false, indexedChunks: 0,
        },
      },
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [preparing], total: 1, hasMore: false, nextCursor: null,
    })));
    renderPage();

    await screen.findByText(preparing.originalFilename!);
    expect(screen.getByLabelText("解析进度")).toHaveTextContent("准备中");
    expect(screen.getByLabelText("解析进度")).not.toHaveTextContent("%");
  });

  it("advances persisted indexing progress and reaches 100% only after publishing all chunks", async () => {
    let indexing: VideoParseTaskView = {
      ...task("INDEXING"),
      chunkCount: 24,
      detail: {
        progress: {
          completedChunks: 24, totalChunks: 24,
          completedRefines: 22, totalRefines: 22,
          completedUnits: 46, totalUnits: 70,
          percent: 95, topologyReady: true, indexedChunks: 0,
        },
        timing: { recorded: true, complete: true, activeExecutions: 0, effectiveDurationMs: 2_000 },
      },
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [indexing], total: 1, hasMore: false, nextCursor: null,
    })));
    const { container } = renderPage();
    const user = userEvent.setup();

    expect(await screen.findByText("正在发布检索结果")).toBeInTheDocument();
    expect(screen.getByLabelText("解析进度")).toHaveTextContent("95%");
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("索引 0 / 24");
    expect(container.querySelector('[class*="pipelineLine"] > span')).toHaveStyle({ width: "95%" });
    expect(screen.getByRole("region", { name: "任务概况" })).toHaveTextContent("进行中1");
    expect(screen.getByRole("region", { name: "任务概况" })).toHaveTextContent("已完成0");
    expect(screen.getByText("00:02")).toBeInTheDocument();
    expect(screen.queryByText("00:02（进行中）")).not.toBeInTheDocument();

    indexing = {
      ...indexing,
      detail: {
        ...indexing.detail,
        progress: { ...indexing.detail.progress!, indexedChunks: 1, completedUnits: 47, percent: 95 + 5 / 24 },
      },
    };
    await user.click(screen.getByRole("button", { name: "同步状态" }));
    await waitFor(() => expect(screen.getByLabelText("解析进度")).toHaveTextContent("95.2%"));
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("索引 1 / 24");
    expect(screen.getByRole("region", { name: "任务概况" })).toHaveTextContent("进行中1");

    indexing = {
      ...indexing,
      status: "COMPLETE",
      detail: {
        ...indexing.detail,
        progress: { ...indexing.detail.progress!, indexedChunks: 24, completedUnits: 70, percent: 100 },
      },
    };
    await user.click(screen.getByRole("button", { name: "同步状态" }));
    await waitFor(() => expect(screen.getByLabelText("解析进度")).toHaveTextContent("100%"));
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("索引 24 / 24");
    expect(screen.getByLabelText("解析任务进度")).toHaveTextContent("完成单元 70 / 70");
    expect(container.querySelector('[class*="pipelineLine"] > span')).toHaveStyle({ width: "100%" });
    expect(screen.getByRole("region", { name: "任务概况" })).toHaveTextContent("已完成1");
  });

  it.each([
    [false, 0],
    [false, 2],
    [true, 1],
    [true, 0],
  ] as const)("uses confirmed timing with complete=%s and active executions=%s", async (complete, activeExecutions) => {
    const parsing: VideoParseTaskView = {
      ...task("PARSING"),
      processingDurationMs: 90_000,
      detail: {
        timing: { recorded: true, complete, activeExecutions, effectiveDurationMs: 1_500 },
      },
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [parsing], total: 1, hasMore: false, nextCursor: null,
    })));
    renderPage();

    expect(await screen.findByText("00:01")).toBeInTheDocument();
    expect(screen.queryByText("01:30")).not.toBeInTheDocument();
    expect(screen.getByText("有效解析时长").parentElement).toHaveAttribute("title", "仅统计实际解析时间，排队和停机不计入");
  });

  it.each([false, true])("does not present historical zero timing as recorded when timing metadata exists=%s", async (hasTiming) => {
    const failed: VideoParseTaskView = {
      ...task("PARSING_FAILED"),
      processingDurationMs: 0,
      processingTimingComplete: true,
      detail: hasTiming ? {
        timing: { recorded: false, complete: false, activeExecutions: 0, effectiveDurationMs: null },
      } : {},
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [failed], total: 1, hasMore: false, nextCursor: null,
    })));
    renderPage();

    expect(await screen.findByText("未记录")).toBeInTheDocument();
    expect(screen.getByText("有效解析时长").parentElement).not.toHaveTextContent("00:00");
  });

  it("shows an unrecorded pending task as not started", async () => {
    const pending: VideoParseTaskView = {
      ...task("PENDING"),
      detail: {
        timing: { recorded: false, complete: false, activeExecutions: 0, effectiveDurationMs: null },
      },
    };
    vi.stubGlobal("fetch", vi.fn(async () => response({
      items: [pending], total: 1, hasMore: false, nextCursor: null,
    })));
    renderPage();

    expect(await screen.findByText("未开始")).toBeInTheDocument();
  });
});

describe("V2 task actions", () => {
  it("offers starting an unaccepted pending task without exposing takeover internals", async () => {
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
      name: `开始解析 ${pending.originalFilename}`,
    }));
    expect(screen.getByText("开始解析这一集？")).toBeInTheDocument();
    expect(screen.getByText("将开始解析这集视频，新的模型调用会计入用量。")).toBeInTheDocument();
    expect(screen.getByRole("button", {
      name: `确认开始解析 ${pending.originalFilename}`,
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
    expect(uploadActions.syncRequeuedParse).toHaveBeenCalledWith(
      expect.objectContaining({
        videoParseTaskId: failed.videoParseTaskId,
        uploadSessionId: failed.uploadSessionId,
        originalFilename: failed.originalFilename,
        status: "PENDING",
        attemptCount: 3,
        maxAttempts: 4,
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
    expect(screen.getByText("将从视频开头重新解析，新的模型调用会计入用量。")).toBeInTheDocument();
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
