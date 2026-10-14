import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    render(<ParseUsageDetails runId="job-A" status="COMPLETE" />, { wrapper: wrapper() });
    expect(getUsageOverview).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("model-A")).toBeInTheDocument();
    expect(getUsageOverview).toHaveBeenCalledOnce();
    expect(getUsageOverview).toHaveBeenCalledWith({ runId: "job-A" }, expect.any(AbortSignal));
    expect(screen.getByText("解析 · 视频时间线解析")).toBeInTheDocument();
    expect(screen.getByText("1,000")).toBeInTheDocument();
    expect(screen.getByText("80")).toBeInTheDocument();
    expect(screen.getAllByText("未计价")).toHaveLength(2);
    expect(screen.getByText("未记录")).toBeInTheDocument();
  });

  it("keeps missing usage and truncation explicit", async () => {
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ truncated: true, totals: totals({ totalTokens: null, missingUsageCount: 1 }) }));
    render(<ParseUsageDetails runId="job-A" status="COMPLETE" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText(/1 条调用记录缺少完整 Token 用量/)).toBeInTheDocument();
    expect(screen.getByText("已知 Token 小计")).toBeInTheDocument();
    expect(screen.getByText(/当前读取结果被截断/)).toBeInTheDocument();
  });

  it("displays unknown metadata without labeling an unknown category as parsing", async () => {
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ models: [{ ...totals(), provider: null, model: null, category: null, operation: null }] }));
    render(<ParseUsageDetails runId="job-A" status="COMPLETE" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("未知模型")).toBeInTheDocument();
    expect(screen.getByText("未知服务商")).toBeInTheDocument();
    expect(screen.getByText("未知用途")).toBeInTheDocument();
    expect(screen.queryByText("解析 · 未知用途")).not.toBeInTheDocument();
  });

  it("does not reuse another run's statistics when the selected task changes", async () => {
    vi.mocked(getUsageOverview).mockImplementation(async (options) => overview(options?.runId === "job-B" ? { models: [], totals: totals({ callCount: 0, inputTokens: 0, outputTokens: 0, totalTokens: 0, knownTokens: 0 }) } : {}));
    const view = render(<ParseUsageDetails runId="job-A" status="COMPLETE" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("model-A")).toBeInTheDocument();
    view.rerender(<ParseUsageDetails runId="job-B" status="COMPLETE" />);
    expect(await screen.findByText(/job-B · 最近 30 天/)).toBeInTheDocument();
    expect(getUsageOverview).toHaveBeenLastCalledWith({ runId: "job-B" }, expect.any(AbortSignal));
    expect(screen.queryByText("model-A")).not.toBeInTheDocument();
  });

  it("does not display fabricated zero totals when local usage is unavailable", async () => {
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ status: "unavailable", totals: totals({ callCount: null }), models: [] }));
    render(<ParseUsageDetails runId="job-A" status="PARSING_FAILED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText(/模型消耗暂时无法读取/)).toBeInTheDocument();
    expect(screen.queryByLabelText("本次任务模型消耗合计")).not.toBeInTheDocument();
  });

  it("allows retrying a failed read for the same run", async () => {
    vi.mocked(getUsageOverview).mockRejectedValueOnce(new ApiClientError(404, { code: "NOT_FOUND", message: "无法读取消耗" })).mockResolvedValueOnce(overview());
    render(<ParseUsageDetails runId="job-A" status="PARSING_FAILED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取消耗");
    await userEvent.click(screen.getByRole("button", { name: "重新读取消耗" }));
    expect(await screen.findByText("model-A")).toBeInTheDocument();
    expect(getUsageOverview).toHaveBeenCalledTimes(2);
  });

  it.each(["COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"] as const)("refreshes after an expanded V2 task becomes %s", async (status) => {
    vi.mocked(getUsageOverview).mockResolvedValueOnce(overview()).mockResolvedValueOnce(overview({ totals: totals({ totalTokens: 2_500 }) }));
    const view = render(<ParseUsageDetails runId="job-A" status="PARSING" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    await screen.findByText("model-A");
    view.rerender(<ParseUsageDetails runId="job-A" status={status} />);
    await waitFor(() => expect(screen.getByText("2,500")).toBeInTheDocument());
    expect(getUsageOverview).toHaveBeenCalledTimes(2);
  });

  it("does not treat an indexing transition as terminal", async () => {
    const view = render(<ParseUsageDetails runId="task-A" status="PARSING" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    await screen.findByText("model-A");
    view.rerender(<ParseUsageDetails runId="task-A" status="INDEXING" />);
    expect(getUsageOverview).toHaveBeenCalledOnce();
  });

  it("shows rounded API estimates and actual sources without merging equal model names", async () => {
    const cost = { currency: "CNY" as const, estimatedAmount: "0.12345678901234567890", knownAmount: "0.12345678901234567890",
      pricedCallCount: 1, unpricedCallCount: 0, excludedCallCount: 0 };
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ totals: totals({ cost }), models: [
      { ...overview().models[0]!, src: "endpoint-A", cost },
      { ...overview().models[0]!, src: "endpoint-B", cost: null },
    ] }));
    render(<ParseUsageDetails runId="task-A" status="COMPLETE" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("endpoint-A")).toBeInTheDocument();
    expect(screen.getByText("endpoint-B")).toBeInTheDocument();
    expect(screen.getAllByText("model-A")).toHaveLength(2);
    expect(within(screen.getByLabelText("本次任务模型消耗合计")).getByText("¥0.1235")).toBeInTheDocument();
    expect(screen.getByText("未计价")).toBeInTheDocument();
    expect(screen.queryByText("local")).not.toBeInTheDocument();
  });

  it("labels partial API pricing and preserves missing token and truncation notices", async () => {
    const cost = { currency: "CNY" as const, estimatedAmount: null, knownAmount: "0.15",
      pricedCallCount: 1, unpricedCallCount: 2, excludedCallCount: 1 };
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ truncated: true,
      totals: totals({ cost, totalTokens: null, missingUsageCount: 1 }), models: [{ ...overview().models[0]!, cost }],
    }));
    render(<ParseUsageDetails runId="task-A" status="PARSING_FAILED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("已计价API小计")).toBeInTheDocument();
    expect(screen.getAllByText("¥0.15（小计）")).toHaveLength(2);
    expect(screen.getByText(/2 条 API 调用未计价/)).toBeInTheDocument();
    expect(screen.getByText(/1 条调用记录缺少完整 Token 用量/)).toBeInTheDocument();
    expect(screen.getByText(/当前读取结果被截断/)).toBeInTheDocument();
  });

  it("keeps Codex subscription calls token-only without fabricating zero cost or quota", async () => {
    const cost = { currency: "CNY" as const, estimatedAmount: "0", knownAmount: "0",
      pricedCallCount: 0, unpricedCallCount: 0, excludedCallCount: 1 };
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ totals: totals({ cost }),
      models: [{ ...overview().models[0]!, provider: "codex_local", model: "gpt-6-astra", src: "", cost }],
    }));
    render(<ParseUsageDetails runId="task-A" status="COMPLETE" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("gpt-6-astra")).toBeInTheDocument();
    expect(screen.getAllByText("不适用（Codex订阅）")).toHaveLength(2);
    expect(screen.getByText("codex_local")).toBeInTheDocument();
    expect(screen.getByText("未记录")).toBeInTheDocument();
    expect(screen.getByText(/Token 不能直接换算为每周额度百分比/)).toBeInTheDocument();
    expect(screen.queryByText(/^¥/)).not.toBeInTheDocument();
    expect(screen.getAllByText("1,200")).toHaveLength(2);
  });

  it.each([null, "9.99"])("marks row amounts as subtotals when full pricing is unavailable or truncated: %s", async (estimatedAmount) => {
    const cost = { currency: "CNY" as const, estimatedAmount, knownAmount: "0.12",
      pricedCallCount: 1, unpricedCallCount: 0, excludedCallCount: 0 };
    vi.mocked(getUsageOverview).mockResolvedValue(overview({ truncated: estimatedAmount !== null,
      totals: totals({ cost }), models: [{ ...overview().models[0]!, cost }],
    }));
    render(<ParseUsageDetails runId="task-A" status="COMPLETE" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("已计价API小计")).toBeInTheDocument();
    expect(screen.getAllByText("¥0.12（小计）")).toHaveLength(2);
    expect(screen.queryByText(/^¥9.99/)).not.toBeInTheDocument();
  });

  it("polls every 60 seconds while expanded and stops after collapse", async () => {
    vi.useFakeTimers();
    render(<ParseUsageDetails runId="job-A" status="COMPLETE" />, { wrapper: wrapper() });
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
