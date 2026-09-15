import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError, getParseUsage } from "../../api/client";
import type { ParseUsageVO } from "../../api/contracts";
import { ParseUsageDetails } from "./ParseUsageDetails";

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  getParseUsage: vi.fn(),
}));

function usage(jobId = "job-A", overrides: Partial<ParseUsageVO> = {}): ParseUsageVO {
  return {
    jobId, videoId: "video-A", seriesId: "series", seriesName: "测试动画", episodeOrder: 2, originalFilename: "episode-02.mp4", currency: "CNY", coverage: "recorded",
    totalTokens: 1_260, estimatedCostCny: "0.00000004", callCount: 2, missingUsageCount: 0, unpricedCallCount: 0,
    items: [{ provider: "provider-A", model: "model-A", category: "parse", operation: "video_timeline", callCount: 1, inputTokens: 1_000, outputTokens: 200, cachedInputTokens: 80, reasoningTokens: 60, totalTokens: 1_200, estimatedCostCny: "0.00000003", missingUsageCount: 0, unpricedCallCount: 0 },
      { provider: "provider-B", model: "embedding-B", category: "embedding", operation: "text_embedding", callCount: 1, inputTokens: 60, outputTokens: 0, cachedInputTokens: null, reasoningTokens: null, totalTokens: 60, estimatedCostCny: "0.00000001", missingUsageCount: 0, unpricedCallCount: 0 }],
    ...overrides,
  };
}

function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: PropsWithChildren) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => { vi.clearAllMocks(); vi.mocked(getParseUsage).mockImplementation(async (jobId) => usage(jobId)); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("parse model usage details", () => {
  it("loads only when expanded and displays real model roles, token subsets and exact tiny CNY costs", async () => {
    render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    expect(getParseUsage).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("model-A")).toBeInTheDocument();
    expect(getParseUsage).toHaveBeenCalledOnce();
    expect(getParseUsage).toHaveBeenCalledWith("job-A");
    expect(screen.getByText("解析 · 视频时间线解析")).toBeInTheDocument();
    expect(screen.getByText("向量化 · 文本向量化")).toBeInTheDocument();
    expect(screen.getByText("1,260")).toBeInTheDocument();
    expect(screen.getByText("1,000")).toBeInTheDocument();
    expect(screen.getByText("80")).toBeInTheDocument();
    expect(screen.getByText("¥0.00000004")).toBeInTheDocument();
    expect(screen.getByText("¥0.00000003")).toBeInTheDocument();
    expect(screen.queryByText("¥0.00")).not.toBeInTheDocument();
  });

  it("keeps missing usage and unknown prices explicit instead of manufacturing zero totals", async () => {
    const incomplete = usage("job-A", { coverage: "partial", totalTokens: null, estimatedCostCny: null, missingUsageCount: 1, unpricedCallCount: 1 });
    incomplete.items[0] = { ...incomplete.items[0]!, inputTokens: null, outputTokens: null, cachedInputTokens: null, reasoningTokens: null, totalTokens: null, estimatedCostCny: null, missingUsageCount: 1 };
    incomplete.items[1] = { ...incomplete.items[1]!, estimatedCostCny: null, unpricedCallCount: 1 };
    vi.mocked(getParseUsage).mockResolvedValue(incomplete);
    render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText(/历史未记录用量不会计为零/)).toBeInTheDocument();
    expect(screen.getByText(/1 次调用未返回完整 Token 用量/)).toBeInTheDocument();
    expect(screen.getByText(/1 次调用缺少完整计价信息/)).toBeInTheDocument();
    expect(screen.getAllByText("未记录").length).toBeGreaterThan(1);
    expect(screen.getAllByText("未估算").length).toBeGreaterThan(1);
    expect(screen.queryByText(/^¥/)).not.toBeInTheDocument();
  });

  it("does not reuse another job's consumption when the selected job changes", async () => {
    vi.mocked(getParseUsage).mockImplementation(async (jobId) => usage(jobId, jobId === "job-B" ? { originalFilename: "episode-B.mp4", items: [], callCount: 0, totalTokens: 0, estimatedCostCny: "0", coverage: "recorded" } : {}));
    const view = render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("model-A")).toBeInTheDocument();
    view.rerender(<ParseUsageDetails jobId="job-B" status="SUCCEEDED" />);
    expect(await screen.findByText(/episode-B.mp4/)).toBeInTheDocument();
    expect(getParseUsage).toHaveBeenCalledWith("job-B");
    expect(screen.queryByText("model-A")).not.toBeInTheDocument();
    expect(screen.getByText("¥0.00")).toBeInTheDocument();
    expect(screen.getByText("本次任务已跟踪，尚无模型调用。")).toBeInTheDocument();
  });

  it("shows a failed read and allows retrying the same task without hiding an unknown history", async () => {
    vi.mocked(getParseUsage).mockRejectedValueOnce(new ApiClientError(404, { code: "NOT_FOUND", message: "无法读取消耗" })).mockResolvedValueOnce(usage("job-A", { coverage: "unknown", callCount: 0, totalTokens: null, estimatedCostCny: null, items: [] }));
    render(<ParseUsageDetails jobId="job-A" status="FAILED" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取消耗");
    expect(getParseUsage).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole("button", { name: "重新读取消耗" }));
    expect(await screen.findByText("此任务没有模型消耗记录，无法还原历史 Token 和费用。")).toBeInTheDocument();
    expect(getParseUsage).toHaveBeenCalledTimes(2);
    expect(getParseUsage).toHaveBeenLastCalledWith("job-A");
    expect(screen.queryByText("¥0.00")).not.toBeInTheDocument();
  });

  it("refreshes the final usage when an expanded running task becomes terminal", async () => {
    vi.mocked(getParseUsage).mockResolvedValueOnce(usage("job-A", { totalTokens: 1_260 })).mockResolvedValueOnce(usage("job-A", { totalTokens: 2_500, estimatedCostCny: "1.2500" }));
    const view = render(<ParseUsageDetails jobId="job-A" status="RUNNING" />, { wrapper: wrapper() });
    await userEvent.click(screen.getByText("模型消耗"));
    expect(await screen.findByText("1,260")).toBeInTheDocument();
    view.rerender(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />);
    await waitFor(() => expect(screen.getByText("2,500")).toBeInTheDocument());
    expect(screen.getByText("¥1.2500")).toBeInTheDocument();
    expect(getParseUsage).toHaveBeenCalledTimes(2);
  });

  it("keeps reading late usage for a terminal partial task and stops once recording is complete", async () => {
    vi.useFakeTimers();
    vi.mocked(getParseUsage).mockResolvedValueOnce(usage("job-A", { coverage: "partial", totalTokens: null, estimatedCostCny: null }))
      .mockResolvedValueOnce(usage("job-A", { totalTokens: 2_500, estimatedCostCny: "1.2500" }));
    render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    const details = screen.getByText("模型消耗").closest("details")!;
    await act(async () => {
      details.open = true;
      fireEvent(details, new Event("toggle"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(getParseUsage).toHaveBeenCalledOnce();
    expect(screen.getByText(/历史未记录用量不会计为零/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(14_999); });
    expect(getParseUsage).toHaveBeenCalledOnce();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(getParseUsage).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText("2,500")).toBeInTheDocument();
    expect(screen.getByText("¥1.2500")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(getParseUsage).toHaveBeenCalledTimes(2);
  });

  it("stops polling a partial task when the usage details are closed", async () => {
    vi.useFakeTimers();
    vi.mocked(getParseUsage).mockResolvedValue(usage("job-A", { coverage: "partial", totalTokens: null, estimatedCostCny: null }));
    render(<ParseUsageDetails jobId="job-A" status="SUCCEEDED" />, { wrapper: wrapper() });
    const details = screen.getByText("模型消耗").closest("details")!;
    await act(async () => {
      details.open = true;
      fireEvent(details, new Event("toggle"));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(getParseUsage).toHaveBeenCalledOnce();
    await act(async () => {
      details.open = false;
      fireEvent(details, new Event("toggle"));
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(getParseUsage).toHaveBeenCalledOnce();
    expect(screen.queryByText("model-A")).not.toBeInTheDocument();
  });
});
