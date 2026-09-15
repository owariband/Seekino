import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ParseJobView, ParseUsageVO } from "../../api/contracts";
import { formatCostCny } from "../../shared/format";
import { UsagePage } from "./UsagePage";
import { summarizeUsage } from "./usage";

function job(id: string, seriesId = "series-A"): ParseJobView {
  return {
    jobId: id, workspaceId: "local-default", seriesId, videoId: `video-${id}`, seriesName: seriesId,
    episodeOrder: 1, originalFilename: `episode-${id}.mp4`, pipelineVersion: "v1", retryOfJobId: null,
    status: "SUCCEEDED", stage: "COMPLETED", progress: 100, attemptCount: 2, maxAttempts: 3,
    availableAt: null, startedAt: 1, completedAt: 2, estimatedRemainingMs: null, canRetry: false, canCancel: false,
    lastError: null, version: 1, checkpoints: [], createdAt: 1, updatedAt: 2,
  };
}

function usage(item: ParseJobView, overrides: Partial<ParseUsageVO> = {}): ParseUsageVO {
  return {
    jobId: item.jobId, videoId: item.videoId, seriesId: item.seriesId, seriesName: item.seriesName,
    episodeOrder: item.episodeOrder, originalFilename: item.originalFilename, currency: "CNY", coverage: "recorded",
    totalTokens: 1_200, estimatedCostCny: "0.00000004", callCount: 1, missingUsageCount: 0, unpricedCallCount: 0,
    items: [{ provider: "provider-A", model: "model-A", category: "parse", operation: "video_timeline", callCount: 1,
      inputTokens: 1_000, outputTokens: 200, cachedInputTokens: 80, reasoningTokens: 60, totalTokens: 1_200,
      estimatedCostCny: "0.00000004", missingUsageCount: 0, unpricedCallCount: 0 }],
    ...overrides,
  };
}

function response(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

function renderPage(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return { queryClient, ...render(<QueryClientProvider client={queryClient}><MemoryRouter><UsagePage /></MemoryRouter></QueryClientProvider>) };
}

afterEach(() => vi.unstubAllGlobals());

describe("usage aggregation", () => {
  it("sums exact tiny decimal costs and token subsets without double counting", () => {
    const first = job("first");
    const second = job("second");
    const secondUsage = usage(second, { estimatedCostCny: "0.00000005" });
    secondUsage.items[0]!.estimatedCostCny = "0.00000005";
    const total = summarizeUsage([{ job: first, usage: usage(first), error: null }, { job: second, usage: secondUsage, error: null }]);
    expect(total.totalTokens).toBe(2_400);
    expect(total.estimatedCostCny).toBe("0.00000009");
    expect(formatCostCny(total.estimatedCostCny)).toBe("¥0.00000009");
    expect(total.models).toHaveLength(1);
    expect(total.models[0]).toMatchObject({ inputTokens: 2_000, outputTokens: 400, cachedInputTokens: 160, reasoningTokens: 120, totalTokens: 2_400, estimatedCostCny: "0.00000009" });
  });

  it("preserves precision when decimal costs carry into the integer part", () => {
    const item = job("first");
    const total = summarizeUsage([{ job: item, usage: usage(item, { estimatedCostCny: "999.99999999" }), error: null }, { job: job("second"), usage: usage(job("second"), { estimatedCostCny: "0.00000001" }), error: null }]);
    expect(total.estimatedCostCny).toBe("1000.00000000");
    expect(formatCostCny(total.estimatedCostCny)).toBe("¥1,000.00000000");
  });

  it("keeps incomplete totals unknown and retains only explicitly known subtotals", () => {
    const first = job("first");
    const second = job("second");
    const incomplete = usage(second, { coverage: "partial", totalTokens: null, estimatedCostCny: null, missingUsageCount: 1 });
    incomplete.items[0] = { ...incomplete.items[0]!, inputTokens: null, outputTokens: null, totalTokens: null, estimatedCostCny: null, missingUsageCount: 1 };
    const total = summarizeUsage([{ job: first, usage: usage(first), error: null }, { job: second, usage: incomplete, error: null }]);
    expect(total.totalTokens).toBeNull();
    expect(total.estimatedCostCny).toBeNull();
    expect(total.knownTokens).toBe(1_200);
    expect(total.knownCostCny).toBe("0.00000004");
    expect(total.models[0]!.totalTokens).toBeNull();
    expect(total.models[0]!.estimatedCostCny).toBeNull();
  });
});

describe("model usage page", () => {
  it("reads every page, deduplicates jobs, aggregates models and filters tasks by series", async () => {
    const first = job("first");
    const second = job("second", "series-B");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/list") return response(url.searchParams.has("cursor")
        ? { items: [first, second], hasMore: false, nextCursor: null }
        : { items: [first], hasMore: true, nextCursor: "next-page" });
      if (url.pathname === "/api/jobs/usage/get") return response(usage(url.searchParams.get("jobId") === "first" ? first : second));
      throw new Error(`Unexpected API: ${url.pathname}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    expect(await screen.findByText("2 个解析任务")).toBeInTheDocument();
    expect(within(screen.getByLabelText("模型消耗概况")).getByText("2,400")).toBeInTheDocument();
    expect(within(screen.getByLabelText("模型消耗概况")).getByText("¥0.00000008")).toBeInTheDocument();
    expect(screen.getByText(/不含 Agent 聊天/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/usage/get"))).toHaveLength(2);
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("cursor=next-page"))).toBe(true);
    expect(screen.getByText("model-A")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("视频系列"), "series-B");
    expect(within(screen.getByLabelText("模型消耗概况")).getByText("1,200")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "按任务" }));
    expect(screen.getByText(second.originalFilename)).toBeInTheDocument();
    expect(screen.queryByText(first.originalFilename)).not.toBeInTheDocument();
  });

  it("shows read failures and unknown history, then refreshes cached usage on explicit retry", async () => {
    const jobs = [job("good"), job("failed"), job("old")];
    let failedOnce = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/list") return response({ items: jobs, hasMore: false, nextCursor: null });
      const item = jobs.find(({ jobId }) => jobId === url.searchParams.get("jobId"))!;
      if (item.jobId === "failed" && !failedOnce) { failedOnce = true; return response({ code: "NOT_FOUND", message: "用量暂不可读" }, 404); }
      return response(item.jobId === "old" ? usage(item, { coverage: "unknown", totalTokens: null, estimatedCostCny: null, callCount: 0, items: [] }) : usage(item));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    expect(await screen.findByText(/1 个任务的用量读取失败/)).toBeInTheDocument();
    expect(screen.getByText(/1 个历史任务没有用量记录/)).toBeInTheDocument();
    const overview = within(screen.getByLabelText("模型消耗概况"));
    expect(overview.getByText("未记录")).toBeInTheDocument();
    expect(overview.getByText("未估算")).toBeInTheDocument();
    expect(overview.queryByText("¥0.00")).not.toBeInTheDocument();
    expect(overview.getByText(/已知合计 1,200/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "按任务" }));
    expect(screen.getByText("用量暂不可读")).toBeInTheDocument();
    expect(screen.getByText("历史未记录")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "刷新用量" }));
    await waitFor(() => expect(screen.queryByText(/1 个任务的用量读取失败/)).not.toBeInTheDocument());
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("jobId=good"))).toHaveLength(2);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("jobId=failed"))).toHaveLength(2);
    expect(screen.getByText(/已知合计 2,400/)).toBeInTheDocument();
  });

  it("keeps the page accessible with no jobs and does not request invented usage", async () => {
    const fetchMock = vi.fn(async () => response({ items: [], hasMore: false, nextCursor: null }));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    expect(await screen.findByRole("heading", { name: "暂无解析任务" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前往资料库" })).toHaveAttribute("href", "/library");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("bounds concurrent reads to six and reuses finalized usage on revisiting", async () => {
    const jobs = Array.from({ length: 8 }, (_, index) => job(String(index)));
    let active = 0;
    let peak = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/list") return response({ items: jobs, hasMore: false, nextCursor: null });
      active += 1;
      peak = Math.max(peak, active);
      await new Promise((resolve) => setTimeout(resolve, 10));
      active -= 1;
      return response(usage(jobs.find(({ jobId }) => jobId === url.searchParams.get("jobId"))!));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { unmount, queryClient } = renderPage();
    expect(await screen.findByText("8 个解析任务")).toBeInTheDocument();
    expect(peak).toBe(6);
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/usage/get"))).toHaveLength(8);
    unmount();
    renderPage(queryClient);
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/jobs/list"))).toHaveLength(2));
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新用量" })).toBeEnabled());
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/usage/get"))).toHaveLength(8);
  });
});
