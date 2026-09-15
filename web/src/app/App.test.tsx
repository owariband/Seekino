import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentMessageView, SeriesView } from "../api/contracts";
import { App } from "./App";
import { ThemeProvider } from "./theme";
import { WorkspaceProvider } from "./workspace";

const apiSeries: SeriesView = {
  seriesId: "series-from-api",
  workspaceId: "local-default",
  name: "接口返回的系列",
  description: "这条资料来自 Series API。",
  status: "READY",
  importantExtraData: null,
  characterBible: [],
  videoCount: 0,
  videos: [],
};

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response;
}

function renderApp() {
  let finishAnswer: (() => void) | undefined;
  const now = "2026-09-15T00:00:00Z";
  const session = { sessionId: "session", workspaceId: "local-default", name: "Test", conversationCount: 1, createdAt: now, updatedAt: now };
  const conversation = { conversationId: "conversation", sessionId: "session", workspaceId: "local-default", title: "Test", seriesIds: [apiSeries.seriesId], messageCount: 0, lastMessageAt: null, createdAt: now, updatedAt: now };
  const assistantMessage: AgentMessageView = {
    messageId: "assistant", conversationId: "conversation", sequenceNo: 1, role: "ASSISTANT", status: "PROCESSING", content: null,
    modelName: null, replyToMessageId: "user", retrievalQuery: null, searchResult: null, lastError: null, createdAt: now, updatedAt: now,
  };
  const userMessage = { ...assistantMessage, messageId: "user", sequenceNo: 0, role: "USER", status: "COMPLETED", content: "雨天门口" };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/healthz")) return jsonResponse({ status: "ok" });
    if (url.includes("/api/series/list")) return jsonResponse([apiSeries]);
    if (url.includes("/api/series/get")) return jsonResponse(apiSeries);
    if (url.includes("/api/jobs/list")) return jsonResponse([]);
    if (url.includes("/api/agent/sessions/")) return jsonResponse(session);
    if (url.includes("/api/agent/conversations/")) return jsonResponse(conversation);
    if (url.includes("/api/agent/messages/list")) return jsonResponse([]);
    if (url.includes("/api/agent/messages/send")) {
      const body = new ReadableStream<Uint8Array>({ start(controller) {
        const emit = (type: string, payload: unknown) => controller.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`));
        const base = { conversationId: "conversation", messageId: "assistant" };
        emit("answer.started", { ...base, sequence: 1, userMessage, assistantMessage });
        emit("answer.evidence", { ...base, sequence: 2, hits: [] });
        emit("answer.delta", { ...base, sequence: 3, delta: "这是 Agent Chat" });
        finishAnswer = () => {
          emit("answer.delta", { ...base, sequence: 4, delta: " 保存的完整回答。" });
          emit("answer.completed", { ...base, sequence: 5, userMessage, assistantMessage: { ...assistantMessage, status: "COMPLETED", content: "这是 Agent Chat 保存的完整回答。" } });
          controller.close();
        };
      } });
      return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    }
    throw new Error(`Unexpected API request in test: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("EventSource", class extends EventTarget { close() {} });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <WorkspaceProvider>
          <MemoryRouter initialEntries={["/search"]}>
            <App />
          </MemoryRouter>
        </WorkspaceProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
  return { ...view, fetchMock, finishAnswer: () => finishAnswer?.() };
}

describe("application shell", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("starts with an empty queue and only displays files selected by the user", async () => {
    const { fetchMock } = renderApp();
    const user = userEvent.setup();

    expect(await screen.findByRole("option", { name: apiSeries.name })).toBeInTheDocument();
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) => String(input).includes("/api/series/list")),
      ).toBe(true);
      expect(
        fetchMock.mock.calls.some(([input]) => String(input).includes("/api/series/get")),
      ).toBe(true);
    });

    const uploadDock = await screen.findByRole("button", { name: /并行传输/ });
    await user.click(uploadDock);

    expect(await screen.findByRole("dialog", { name: /把每一集/ })).toBeInTheDocument();
    expect(screen.getByText("还没有传输记录。把视频拖到上方即可开始。")).toBeInTheDocument();
    expect(screen.queryAllByRole("progressbar")).toHaveLength(0);

    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    await user.upload(input!, [
      new File(["first"], "episode-01.mp4", { type: "video/mp4" }),
      new File(["second"], "episode-02.mov", { type: "video/quicktime" }),
      new File(["third"], "episode-03.avi", { type: "video/x-msvideo" }),
    ]);

    expect(screen.getByText("episode-01.mp4")).toBeInTheDocument();
    expect(screen.getByText("episode-02.mov")).toBeInTheDocument();
    expect(screen.getByText("episode-03.avi")).toBeInTheDocument();
  });

  it("renders every primary route and switches to the night theme", async () => {
    renderApp();
    const user = userEvent.setup();

    await user.click(screen.getAllByRole("link", { name: /资料库/ })[0]!);
    expect(await screen.findByRole("heading", { name: "资料库", level: 1 })).toBeInTheDocument();

    await user.click(screen.getAllByRole("link", { name: /解析任务/ })[0]!);
    expect(await screen.findByRole("heading", { name: "解析任务", level: 1 })).toBeInTheDocument();

    await user.click(screen.getAllByRole("link", { name: /设置/ })[0]!);
    expect(await screen.findByRole("heading", { name: "设置", level: 1 })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "切换至黑夜模式" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("creates a scoped Conversation and renders a real delta before durable completion", async () => {
    const { fetchMock, finishAnswer } = renderApp();
    const user = userEvent.setup();

    const composer = await screen.findByRole("textbox", {
      name: "向画面札记 Agent 提问",
    });
    await user.type(composer, "雨天门口");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled(),
    );
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(await screen.findByText("这是 Agent Chat")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    act(finishAnswer);
    expect(await screen.findByText("这是 Agent Chat 保存的完整回答。")).toBeInTheDocument();
    const creation = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/agent/conversations/create"));
    expect(JSON.parse(String(creation?.[1]?.body)).seriesIds).toEqual([apiSeries.seriesId]);
    const sendCall = fetchMock.mock.calls.find(([input]) =>
      String(input).includes("/api/agent/messages/send?conversationId=conversation"),
    );
    expect(sendCall).toBeDefined();
    expect(new Headers(sendCall?.[1]?.headers).get("Accept")).toBe("text/event-stream");
    expect(JSON.parse(String(sendCall?.[1]?.body))).toEqual({
      workspaceId: "local-default",
      content: "雨天门口",
      topK: 10,
    });
  });
});
