import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { UsageOverviewVO, UsageTotals } from "../../api/contracts";
import { UsagePage } from "./UsagePage";

const workspace = vi.hoisted(() => ({ displayMode: "debug" as "release" | "debug" }));

vi.mock("../../app/workspace", () => ({
  useWorkspace: () => workspace,
}));

function totals(overrides: Partial<UsageTotals> = {}): UsageTotals {
  return {
    callCount: 2, inputTokens: 1_000, outputTokens: 200, cachedInputTokens: 80, cacheWriteInputTokens: 40,
    reasoningTokens: 60, totalTokens: 1_200, knownTokens: 1_200,
    missingUsageCount: 0,
    ...overrides,
  };
}

function overview(overrides: Partial<UsageOverviewVO> = {}): UsageOverviewVO {
  return {
    source: "local", status: "available", message: null,
    fromStartTime: 1_759_968_000_000, toStartTime: 1_762_560_000_000, fetchedAt: 1_762_560_000_000, truncated: false,
    totals: totals(),
    models: [{ ...totals({ callCount: 1 }), provider: "provider-A", model: "model-A", category: "parse", operation: "video_timeline" }],
    runs: [{ ...totals({ callCount: 1 }), runId: "parse-A", taskType: "video_parse", seriesId: "series-A", videoId: "video-A" },
      { ...totals({ callCount: 1 }), runId: "chat-B", taskType: "chat_retrieval", seriesId: "series-B", videoId: null }],
    ...overrides,
  };
}

function response(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

function renderPage(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return { queryClient, ...render(<QueryClientProvider client={queryClient}><MemoryRouter><UsagePage /></MemoryRouter></QueryClientProvider>) };
}

afterEach(() => { cleanup(); workspace.displayMode = "debug"; vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("local Token usage page", () => {
  it("keeps overall Tokens and completeness visible in release without internal call details", async () => {
    workspace.displayMode = "release";
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({
      totals: totals({ totalTokens: null, missingUsageCount: 1 }), truncated: true,
    }))));
    renderPage();

    expect(await screen.findByText(/1 条调用记录缺少完整 Token 用量/)).toBeInTheDocument();
    const summary = within(screen.getByLabelText("模型消耗概况"));
    expect(summary.getByText("1,200")).toBeInTheDocument();
    expect(summary.getByText("已知 Token 小计")).toBeInTheDocument();
    expect(screen.getByText(/当前结果被截断/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新用量" })).toBeEnabled();
    expect(screen.queryByLabelText("视频系列")).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "用量统计方式" })).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(/provider-A|model-A|parse-A|chat-B|缓存输入|推理 Token/)).not.toBeInTheDocument();
  });

  it("returns to the complete overview when a debug series filter is hidden by release mode", async () => {
    const filtered = overview({ totals: totals({ callCount: 1, totalTokens: 333, knownTokens: 333 }) });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => response(
      new URL(String(input), "http://localhost").searchParams.get("seriesId") ? filtered : overview(),
    ));
    vi.stubGlobal("fetch", fetchMock);
    const { queryClient, rerender } = renderPage();
    await screen.findByText("model-A");
    await userEvent.selectOptions(screen.getByLabelText("视频系列"), "series-A");
    expect(await screen.findByText("1 条已记录调用")).toBeInTheDocument();

    workspace.displayMode = "release";
    rerender(<QueryClientProvider client={queryClient}><MemoryRouter><UsagePage /></MemoryRouter></QueryClientProvider>);
    expect(screen.getByText("2 条已记录调用")).toBeInTheDocument();
    expect(within(screen.getByLabelText("模型消耗概况")).getByText("1,200")).toBeInTheDocument();
    expect(screen.queryByLabelText("视频系列")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("reads one overview and displays server Token totals in both views", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => response(overview()));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    expect(await screen.findByText("2 条已记录调用")).toBeInTheDocument();
    const summary = within(screen.getByLabelText("模型消耗概况"));
    expect(summary.getByText("1,200")).toBeInTheDocument();
    expect(screen.getByText(/最近 30 天/)).toBeInTheDocument();
    expect(screen.getByText(/解析、向量化与 Chat/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/usage/overview?tenantId=local-default");
    expect(screen.queryByText(/人民币/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Langfuse|USD|费用/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "任务与 Chat" }));
    expect(screen.getByText("parse-A")).toBeInTheDocument();
    expect(screen.getByText("chat-B")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("requests a series filter from the server while retaining the complete series options", async () => {
    const filtered = overview({ totals: totals({ callCount: 1, totalTokens: 333, knownTokens: 333 }), runs: [overview().runs[1]!] });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => response(new URL(String(input), "http://localhost").searchParams.get("seriesId") ? filtered : overview()));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await screen.findByText("model-A");
    await userEvent.selectOptions(screen.getByLabelText("视频系列"), "series-B");
    expect(await screen.findByText("1 条已记录调用")).toBeInTheDocument();
    expect(within(screen.getByLabelText("模型消耗概况")).getByText("333")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "series-A" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain("seriesId=series-B");
    await userEvent.click(screen.getByRole("button", { name: "任务与 Chat" }));
    expect(screen.getByText("chat-B")).toBeInTheDocument();
    expect(screen.queryByText("parse-A")).not.toBeInTheDocument();
  });

  it("shows explicitly known subtotals for incomplete usage", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ totals: totals({ totalTokens: null, missingUsageCount: 1 }) }))));
    renderPage();
    expect(await screen.findByText(/1 条调用记录缺少完整 Token 用量/)).toBeInTheDocument();
    const summary = within(screen.getByLabelText("模型消耗概况"));
    expect(summary.getByText("已知 Token 小计")).toBeInTheDocument();
    expect(summary.getByText("1,200")).toBeInTheDocument();
    expect(summary.getByText("记录不完整，不能代表总消耗")).toBeInTheDocument();
  });

  it("labels truncated results as partial statistics", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ truncated: true, totals: totals({ callCount: 0 }), models: [], runs: [] }))));
    renderPage();
    expect(await screen.findByText(/当前结果被截断/)).toBeInTheDocument();
    expect(screen.getByText("当前已读取 0 条调用记录（结果截断）")).toBeInTheDocument();
    expect(screen.getByText("已知 Token 小计")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "当前窗口暂无已记录调用" })).not.toBeInTheDocument();
  });

  it("keeps unknown subsets distinct from confirmed core totals", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ models: [{ ...overview().models[0]!, cachedInputTokens: null, reasoningTokens: null }] }))));
    renderPage();
    await screen.findByText("model-A");
    const table = within(screen.getByRole("region", { name: "模型用量明细" }));
    expect(table.getAllByText("—")).toHaveLength(2);
    expect(table.getByText("1,200")).toBeInTheDocument();
    expect(screen.queryByText(/条调用记录缺少完整 Token 用量/)).not.toBeInTheDocument();
  });

  it("shows known model and run subtotals when complete usage is unknown", async () => {
    const partial = totals({ callCount: 1, inputTokens: null, outputTokens: null, totalTokens: null, missingUsageCount: 1 });
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ totals: partial,
      models: [{ ...overview().models[0]!, ...partial }],
      runs: [{ ...overview().runs[1]!, ...partial }],
    }))));
    renderPage();
    expect(await screen.findByText("已知小计 1,200")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "模型用量明细" })).getAllByText("—")).toHaveLength(3);
    await userEvent.click(screen.getByRole("button", { name: "任务与 Chat" }));
    expect(screen.getByText("已知小计 1,200")).toBeInTheDocument();
    expect(screen.getByText("记录不完整")).toBeInTheDocument();
  });

  it("keeps unknown model metadata visible without inventing a provider or purpose", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ models: [{ ...totals(), provider: null, model: null, category: null, operation: null }] }))));
    renderPage();
    expect(await screen.findByText("未知模型")).toBeInTheDocument();
    expect(screen.getByText("未知服务商")).toBeInTheDocument();
    expect(screen.getByText("未知用途")).toBeInTheDocument();
    expect(screen.getByText("未分类")).toBeInTheDocument();
  });

  it("does not display zero success when usage is unavailable", async () => {
    const status = "unavailable" as const;
    vi.stubGlobal("fetch", vi.fn(async () => response(overview({ status, totals: totals({ callCount: null }), models: [], runs: [], message: "配置或连接暂不可用" }))));
    renderPage();
    expect(await screen.findByText("统计暂不可用")).toBeInTheDocument();
    const summary = within(screen.getByLabelText("模型消耗概况"));
    expect(summary.getAllByText("—")).toHaveLength(3);
    expect(summary.queryByText("0")).not.toBeInTheDocument();
    expect(screen.queryByText("当前窗口暂无已记录调用")).not.toBeInTheDocument();
  });

  it("shows a failed read and refreshes with a single retry request", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response({ code: "NOT_FOUND", message: "无法读取用量" }, 404)).mockResolvedValueOnce(response(overview()));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取用量");
    expect(within(screen.getByLabelText("模型消耗概况")).getAllByText("—")).toHaveLength(3);
    await userEvent.click(screen.getByRole("button", { name: "刷新用量" }));
    expect(await screen.findByText("2 条已记录调用")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps an empty recorded window distinct from an unavailable backend", async () => {
    const fetchMock = vi.fn(async () => response(overview({ totals: totals({ callCount: 0, inputTokens: 0, outputTokens: 0, totalTokens: 0, knownTokens: 0 }), models: [], runs: [] })));
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    expect(await screen.findByRole("heading", { name: "当前窗口暂无已记录调用" })).toBeInTheDocument();
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
