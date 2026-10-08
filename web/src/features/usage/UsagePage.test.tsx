import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { UsageOverviewVO, UsageTotals } from "../../api/contracts";
import { UsagePage } from "./UsagePage";

function totals(overrides: Partial<UsageTotals> = {}): UsageTotals {
  return {
    callCount: 2, inputTokens: 1_000, outputTokens: 200, cachedInputTokens: 80, cacheWriteInputTokens: 40,
    reasoningTokens: 60, totalTokens: 1_200, knownTokens: 1_200,
    estimatedCostUsd: "0.123456789", knownCostUsd: "0.123456789", missingUsageCount: 0, unpricedCallCount: 0,
    ...overrides,
  };
}

function overview(overrides: Partial<UsageOverviewVO> = {}): UsageOverviewVO {
  return {
    source: "langfuse", currency: "USD", status: "available", message: null,
    fromStartTime: 1_759_968_000_000, toStartTime: 1_762_560_000_000, fetchedAt: 1_762_560_000_000, truncated: false,
    totals: totals(),
    models: [{ ...totals({ callCount: 1, estimatedCostUsd: "0.00000004", knownCostUsd: "0.00000004" }), provider: "provider-A", model: "model-A", category: "parse", operation: "video_timeline" }],
    runs: [{ ...totals({ callCount: 1 }), runId: "parse-A", kind: "parse", seriesId: "series-A", videoId: "video-A" },
      { ...totals({ callCount: 1 }), runId: "chat-B", kind: "chat", seriesId: "series-B", videoId: null }],
    ...overrides,
  };
}

function response(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

function renderPage(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return { queryClient, ...render(<QueryClientProvider client={queryClient}><MemoryRouter><UsagePage /></MemoryRouter></QueryClientProvider>) };
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("Langfuse model usage page", () => {
  it("reads one overview and displays server totals and USD without per-job queries or local sums", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response(overview()));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    expect(await screen.findByText("2 条调用记录已同步")).toBeInTheDocument();
    const summary = within(screen.getByLabelText("模型消耗概况"));
    expect(summary.getByText("1,200")).toBeInTheDocument();
    expect(summary.getByText("$0.123456789")).toBeInTheDocument();
    expect(screen.getByText("$0.00000004")).toBeInTheDocument();
    expect(screen.getByText(/最近 30 天/)).toBeInTheDocument();
    expect(screen.getByText(/解析、向量化与 Chat/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/usage/overview?workspaceId=local-default");
    expect(screen.queryByText(/人民币/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "任务与 Chat" }));
    expect(screen.getByText("parse-A")).toBeInTheDocument();
    expect(screen.getByText("chat-B")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("requests a series filter from the server while retaining the complete series options", async () => {
    const filtered = overview({ totals: totals({ callCount: 1, totalTokens: 333, knownTokens: 333, estimatedCostUsd: "1.000000009" }), runs: [overview().runs[1]!] });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => response(new URL(String(input), "http://localhost").searchParams.get("seriesId") ? filtered : overview()));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await screen.findByText("model-A");
    await userEvent.selectOptions(screen.getByLabelText("视频系列"), "series-B");
    expect(await screen.findByText("1 条调用记录已同步")).toBeInTheDocument();
    expect(within(screen.getByLabelText("模型消耗概况")).getByText("333")).toBeInTheDocument();
    expect(within(screen.getByLabelText("模型消耗概况")).getByText("$1.000000009")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "series-A" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("seriesId=series-B");
    await userEvent.click(screen.getByRole("button", { name: "任务与 Chat" }));
    expect(screen.getByText("chat-B")).toBeInTheDocument();
    expect(screen.queryByText("parse-A")).not.toBeInTheDocument();
  });

  it("shows explicitly known subtotals for missing usage and unpriced calls", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ totals: totals({ totalTokens: null, estimatedCostUsd: null, missingUsageCount: 1, unpricedCallCount: 1 }) }))));
    renderPage();
    expect(await screen.findByText(/1 条调用记录缺少完整 Token 用量/)).toBeInTheDocument();
    expect(screen.getByText(/1 条调用记录尚未计价/)).toBeInTheDocument();
    const summary = within(screen.getByLabelText("模型消耗概况"));
    expect(summary.getByText("已知 Token 小计")).toBeInTheDocument();
    expect(summary.getByText("已计价费用小计（USD）")).toBeInTheDocument();
    expect(summary.getByText("1,200")).toBeInTheDocument();
    expect(summary.getByText("$0.123456789")).toBeInTheDocument();
    expect(summary.getByText("记录不完整，不能代表总消耗")).toBeInTheDocument();
  });

  it("labels truncated results as partial statistics", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ truncated: true, totals: totals({ callCount: 0 }), models: [], runs: [] }))));
    renderPage();
    expect(await screen.findByText(/当前结果被截断/)).toBeInTheDocument();
    expect(screen.getByText("当前已读取 0 条调用记录（结果截断）")).toBeInTheDocument();
    expect(screen.getByText("已知 Token 小计")).toBeInTheDocument();
    expect(screen.getByText("已计价费用小计（USD）")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "当前窗口暂无已同步调用" })).not.toBeInTheDocument();
  });

  it("keeps unknown model metadata visible without inventing a provider or purpose", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ models: [{ ...totals(), provider: null, model: null, category: null, operation: null }] }))));
    renderPage();
    expect(await screen.findByText("未知模型")).toBeInTheDocument();
    expect(screen.getByText("未知服务商")).toBeInTheDocument();
    expect(screen.getByText("未知用途")).toBeInTheDocument();
    expect(screen.getByText("未分类")).toBeInTheDocument();
  });

  it.each(["disabled", "not_configured", "unavailable"] as const)("does not display zero success for %s", async (status) => {
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ status, totals: totals({ callCount: null }), models: [], runs: [], message: "配置或连接暂不可用" }))));
    renderPage();
    expect(await screen.findByText("统计暂不可用")).toBeInTheDocument();
    const summary = within(screen.getByLabelText("模型消耗概况"));
    expect(summary.getAllByText("—")).toHaveLength(3);
    expect(summary.queryByText("0")).not.toBeInTheDocument();
    expect(summary.queryByText("$0.00")).not.toBeInTheDocument();
    expect(screen.queryByText("当前窗口暂无已同步调用")).not.toBeInTheDocument();
    if (status === "not_configured") expect(screen.getByText(/LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY 和 LANGFUSE_BASE_URL/)).toBeInTheDocument();
  });

  it("shows a failed read and refreshes with a single retry request", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response({ code: "NOT_FOUND", message: "无法读取用量" }, 404)).mockResolvedValueOnce(response(overview()));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取用量");
    expect(within(screen.getByLabelText("模型消耗概况")).getAllByText("—")).toHaveLength(3);
    await userEvent.click(screen.getByRole("button", { name: "刷新用量" }));
    expect(await screen.findByText("2 条调用记录已同步")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps an available empty window distinct from an unconfigured backend", async () => {
    const fetchMock = vi.fn(async () => response(overview({ totals: totals({ callCount: 0, totalTokens: 0, knownTokens: 0, estimatedCostUsd: "0", knownCostUsd: "0" }), models: [], runs: [] })));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    expect(await screen.findByRole("heading", { name: "当前窗口暂无已同步调用" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前往资料库" })).toHaveAttribute("href", "/library");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("reuses a fresh overview when revisiting within 30 seconds", async () => {
    const fetchMock = vi.fn(async () => response(overview()));
    vi.stubGlobal("fetch", fetchMock);
    const { unmount, queryClient } = renderPage();
    await screen.findByText("model-A");
    unmount();
    renderPage(queryClient);
    expect(screen.getByText("model-A")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "刷新用量" })).toBeEnabled());
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("polls at 60 seconds rather than refetching continuously", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => response(overview()));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(fetchMock).toHaveBeenCalledOnce();
    await act(async () => { await vi.advanceTimersByTimeAsync(59_999); });
    expect(fetchMock).toHaveBeenCalledOnce();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
