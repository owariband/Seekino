import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError, getUsageOverview } from "../../api/client";
import type { UsageOverviewVO, UsageTotals } from "../../api/contracts";
import { ParseUsageDetails } from "./ParseUsageDetails";

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  getUsageOverview: vi.fn(),
}));

function totals(overrides: Partial<UsageTotals> = {}): UsageTotals {
  return { callCount: 1, inputTokens: 1_000, outputTokens: 200, cachedInputTokens: 80, cacheWriteInputTokens: 20,
    reasoningTokens: 60, totalTokens: 1_200, knownTokens: 1_200, missingUsageCount: 0, ...overrides };
}
function overview(overrides: Partial<UsageOverviewVO> = {}): UsageOverviewVO {
  return { source: "local", status: "available", message: null,
    fromStartTime: 1, toStartTime: 2, fetchedAt: 2, truncated: false, totals: totals(),
    models: [{ ...totals(), provider: "provider-A", model: "model-A", category: "parse", operation: "video_timeline" }],
    runs: [], ...overrides };
}
function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => { vi.clearAllMocks(); vi.mocked(getUsageOverview).mockResolvedValue(overview()); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("local Token parse usage details", () => {
  it("loads the exact run only when expanded and preserves token subsets", async () => {
    render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    expect(getUsageOverview).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("model-A")).toBeInTheDocument();
    expect(getUsageOverview).toHaveBeenCalledOnce();
    expect(getUsageOverview).toHaveBeenCalledWith({ runId: "job-A" }, expect.any(AbortSignal));
    expect(screen.getByText("解析 · 视频时间线解析")).toBeInTheDocument();
    expect(screen.getByText("1,000")).toBeInTheDocument();
    expect(screen.getByText("80")).toBeInTheDocument();
    expect(screen.queryByText(/人民币/)).not.toBeInTheDocument();
  });

  it("keeps missing usage and truncation explicit", async () => {
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ truncated: true, totals: totals({ totalTokens: null, missingUsageCount: 1 }) }));
    render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText(/1 条调用记录缺少完整 Token 用量/)).toBeInTheDocument();
    expect(screen.getByText("已知 Token 小计")).toBeInTheDocument();
    expect(screen.getByText(/当前读取结果被截断/)).toBeInTheDocument();
  });

  it("displays unknown metadata without labeling an unknown category as parsing", async () => {
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ models: [{ ...totals(), provider: null, model: null, category: null, operation: null }] }));
    render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("未知模型")).toBeInTheDocument();
    expect(screen.getByText("未知服务商")).toBeInTheDocument();
    expect(screen.getByText("未知用途")).toBeInTheDocument();
    expect(screen.queryByText("解析 · 未知用途")).not.toBeInTheDocument();
  });

  it("does not reuse another run's statistics when the selected task changes", async () => {
    vi.mocked(getUsageOverview).mockImplementation(async (options) => overview(options?.runId === "job-B" ? { models: [], totals: totals({ callCount: 0, inputTokens: 0, outputTokens: 0, totalTokens: 0, knownTokens: 0 }) } : {}));
    const view = render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("model-A")).toBeInTheDocument();
    view.rerender(<ParseUsageDetails jobId="job-B" status="SUCCEEDED" />);
    expect(await screen.findByText(/job-B · 最近 30 天/)).toBeInTheDocument();
    expect(getUsageOverview).toHaveBeenLastCalledWith({ runId: "job-B" }, expect.any(AbortSignal));
    expect(screen.queryByText("model-A")).not.toBeInTheDocument();
  });

  it("does not display fabricated zero totals when local usage is unavailable", async () => {
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ status: "unavailable", totals: totals({ callCount: null }), models: [] }));
    render(<ParseUsageDetails jobId="job-A" status="FAILED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText(/模型消耗暂时无法读取/)).toBeInTheDocument();
    expect(screen.queryByLabelText("本次任务模型消耗合计")).not.toBeInTheDocument();
  });

  it("allows retrying a failed read for the same run", async () => {
    vi.mocked(getUsageOverview).mockRejectedValueOnce(new ApiClientError(404, { code: "NOT_FOUND", message: "无法读取消耗" })).mockResolvedValueOnce(overview());
    render(<ParseUsageDetails jobId="job-A" status="FAILED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取消耗");
    await userEvent.click(screen.getByRole("button", { name: "重新读取消耗" }));
    expect(await screen.findByText("model-A")).toBeInTheDocument();
    expect(getUsageOverview).toHaveBeenCalledTimes(2);
  });

  it("refreshes after an expanded running task becomes terminal", async () => {
    vi.mocked(getUsageOverview).mockResolvedValueOnce(overview()).mockResolvedValueOnce(overview({ totals: totals({ totalTokens: 2_500 }) }));
    const view = render(<ParseUsageDetails jobId="job-A" status="RUNNING" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    await screen.findByText("model-A");
    view.rerender(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />);
    await waitFor(() => expect(screen.getByText("2,500")).toBeInTheDocument());
    expect(getUsageOverview).toHaveBeenCalledTimes(2);
  });

  it("polls every 60 seconds while expanded and stops after collapse", async () => {
    vi.useFakeTimers();
    render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    const details = screen.getByText("模型消耗").closest("details")!;
    await act(async () => {
      details.open = true;
      fireEvent(details, new Event("toggle"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(getUsageOverview).toHaveBeenCalledOnce();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(getUsageOverview).toHaveBeenCalledTimes(2);
    await act(async () => {
      details.open = false;
      fireEvent(details, new Event("toggle"));
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(getUsageOverview).toHaveBeenCalledTimes(2);
  });
});
