import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentMessageView, SeriesCreateRequest, SeriesUpdateRequest, SeriesView, UploadSessionView } from "../api/contracts";
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
  videos: [], pendingUploads: [], readyVideoCount: 1, failedVideoCount: 0, pendingUploadCount: 0, cover: null, lastSearch: null, createdAt: 1, updatedAt: 1,
};

const pendingUpload: UploadSessionView = {
  uploadSessionId: "upload/session", workspaceId: "local-default", seriesId: apiSeries.seriesId,
  episodeOrder: 1, uploadSequence: 1, originalFilename: "unfinished.mp4", mediaType: "video/mp4",
  totalSizeBytes: 10, contentSha256: null, preferredSegmentSizeBytes: 10, totalSegments: 1,
  status: "UPLOADING", uploadedSegmentCount: 0, uploadedSizeBytes: 0, missingSegmentNumbers: [0],
  completionStage: null, completionProgress: null, estimatedRemainingMs: null, expiresAt: 100,
  videoId: null, jobId: null, lastError: null, version: 1, createdAt: 1, updatedAt: 1,
};

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response;
}

function renderApp(availableSeries: SeriesView[] = [apiSeries]) {
  let createdSeries: SeriesView | null = null;
  let finishAnswer: (() => void) | undefined;
  let messageSends = 0;
  const now = Date.parse("2026-09-15T00:00:00Z");
  const conversation = { conversationId: "conversation", workspaceId: "local-default", title: "Test", messageCount: 0, lastMessagePreview: null, lastMessageAt: null, createdAt: now, updatedAt: now };
  const assistantMessage: AgentMessageView = {
    messageId: "assistant", conversationId: "conversation", seriesIds: [apiSeries.seriesId], sequenceNo: 1, role: "ASSISTANT", status: "PROCESSING", phase: "QUEUED", version: 1, reference: null, content: null,
    modelName: null, replyToMessageId: "user", retrievalQuery: null, searchResult: null, lastError: null, createdAt: now, updatedAt: now,
  };
  const userMessage = { ...assistantMessage, messageId: "user", sequenceNo: 0, role: "USER", status: "COMPLETED", content: "雨天门口" };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.includes("/healthz")) return jsonResponse({ status: "ok" });
    if (url.includes("/api/series/list")) {
      const items = createdSeries ? [...availableSeries, createdSeries] : availableSeries;
      return jsonResponse({ items, total: items.length, hasMore: false, nextCursor: null });
    }
    if (url.includes("/api/series/get")) {
      const seriesId = new URL(url, "http://localhost").searchParams.get("seriesId");
      return jsonResponse(createdSeries?.seriesId === seriesId ? createdSeries : availableSeries.find((item) => item.seriesId === seriesId) ?? apiSeries);
    }
    if (url.includes("/api/series/create")) {
      const request = JSON.parse(String(_init?.body)) as SeriesCreateRequest;
      createdSeries = { ...apiSeries, ...request, seriesId: "created-series-from-api", importantExtraData: request.importantExtraData ?? null, characterBible: request.characterBible ?? [] };
      return jsonResponse(createdSeries);
    }
    if (url.includes("/api/series/update")) {
      const request = JSON.parse(String(_init?.body)) as SeriesUpdateRequest;
      const original = availableSeries.find((item) => item.seriesId === request.seriesId)!;
      const updated: SeriesView = {
        ...original, ...request, updatedAt: now,
        importantExtraData: request.importantExtraData === undefined ? original.importantExtraData : request.importantExtraData === null ? null : { ...request.importantExtraData },
      };
      availableSeries = availableSeries.map((item) => item.seriesId === updated.seriesId ? updated : item);
      return jsonResponse(updated);
    }
    if (url.includes("/api/uploads/delete")) {
      const uploadSessionId = new URL(url, "http://localhost").searchParams.get("uploadSessionId");
      const upload = availableSeries.flatMap((item) => item.pendingUploads).find((item) => item.uploadSessionId === uploadSessionId)!;
      availableSeries = availableSeries.map((item) => ({
        ...item, pendingUploads: item.pendingUploads.filter((entry) => entry.uploadSessionId !== uploadSessionId),
        pendingUploadCount: item.pendingUploads.filter((entry) => entry.uploadSessionId !== uploadSessionId).length,
      }));
      return jsonResponse({ ...upload, status: "EXPIRED" });
    }
    if (url.includes("/api/jobs/list")) return jsonResponse({ items: [], total: 0, hasMore: false, nextCursor: null });
    if (url.includes("/api/agent/conversations/")) return jsonResponse(conversation);
    if (url.includes("/api/agent/messages/list")) return jsonResponse({ items: [], total: 0, hasMore: false, nextCursor: null });
    if (url.includes("/api/agent/messages/send")) {
      const requestedIds = (JSON.parse(String(_init?.body)) as { seriesIds: string[] }).seriesIds;
      const seriesIds = requestedIds.length ? requestedIds : availableSeries.filter((item) => item.status === "READY").map((item) => item.seriesId);
      const index = messageSends++;
      const userSnapshot = { ...userMessage, messageId: `user-${index}`, sequenceNo: index * 2, seriesIds };
      const assistantSnapshot = { ...assistantMessage, messageId: `assistant-${index}`, replyToMessageId: userSnapshot.messageId, sequenceNo: index * 2 + 1, seriesIds };
      const body = new ReadableStream<Uint8Array>({ start(controller) {
        const emit = (type: string, payload: unknown) => controller.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`));
        const base = { occurredAt: now, conversationId: "conversation", messageId: assistantSnapshot.messageId };
        emit("answer.started", { ...base, sequence: 1, userMessage: userSnapshot, assistantMessage: assistantSnapshot });
        emit("answer.evidence", { ...base, sequence: 2, searchResult: { query: "雨天门口", seriesIds, searchedVideoIds: [], partial: false, warnings: [], agentReply: { message: "", resultCount: 0 }, hits: [] } });
        emit("answer.delta", { ...base, sequence: 3, delta: "这是 Agent Chat" });
        finishAnswer = () => {
          emit("answer.delta", { ...base, sequence: 4, delta: " 保存的完整回答。" });
          emit("answer.completed", { ...base, sequence: 5, userMessage: userSnapshot, assistantMessage: { ...assistantSnapshot, version: 2, phase: null, status: "COMPLETED", content: "这是 Agent Chat 保存的完整回答。" } });
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
  return { ...view, fetchMock, queryClient, finishAnswer: () => finishAnswer?.() };
}

describe("application shell", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("starts with an empty queue and only displays files selected by the user", async () => {
    const { fetchMock } = renderApp();
    const user = userEvent.setup();

    await user.click(await screen.findByLabelText("选择检索视频系列"));
    expect(await screen.findByRole("checkbox", { name: new RegExp(apiSeries.name) })).toBeInTheDocument();
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

    expect(screen.getByRole("heading", { name: "名场面检索助手", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("名场面检索助手")).toBeInTheDocument();
    expect(screen.queryByText(/影像札记|问记忆，找画面|当前检索范围/)).not.toBeInTheDocument();

    await user.click(screen.getAllByRole("link", { name: /资料库/ })[0]!);
    expect(await screen.findByRole("heading", { name: "资料库", level: 1 })).toBeInTheDocument();

    await user.click(screen.getAllByRole("link", { name: /解析任务/ })[0]!);
    expect(await screen.findByRole("heading", { name: "解析任务", level: 1 })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "管理上传" }));
    expect(screen.getByRole("dialog", { name: /把每一集/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭上传面板" }));

    await user.click(screen.getAllByRole("link", { name: "模型消耗" })[0]!);
    expect(await screen.findByRole("heading", { name: "模型消耗", level: 1 })).toBeInTheDocument();

    await user.click(screen.getAllByRole("link", { name: /设置/ })[0]!);
    expect(await screen.findByRole("heading", { name: "设置", level: 1 })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "切换至黑夜模式" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it("collapses the desktop navigation without losing the draft and restores the preference", async () => {
    const { unmount } = renderApp();
    const user = userEvent.setup();
    const composer = screen.getByRole("textbox", { name: "向名场面检索助手提问" });
    await user.type(composer, "还未发送的问题");
    await user.click(screen.getByRole("button", { name: "收起导航" }));

    expect(screen.queryByRole("navigation", { name: "主导航" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "展开导航" })).toHaveAttribute("aria-expanded", "false");
    expect(composer).toHaveValue("还未发送的问题");
    expect(window.localStorage.getItem("anivag-sidebar-collapsed")).toBe("true");

    unmount();
    renderApp();
    await user.click(screen.getByRole("button", { name: "展开导航" }));
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起导航" })).toHaveAttribute("aria-expanded", "true");
    expect(window.localStorage.getItem("anivag-sidebar-collapsed")).toBe("false");
  });

  it("keeps the library compact and submits background information through the Series API", async () => {
    const { fetchMock } = renderApp();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("link", { name: "资料库" })[0]!);
    expect(await screen.findByText("暂无背景资料")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "新建视频系列" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "上传视频" })).toHaveLength(1);
    expect(screen.queryByText(/暂无已登记封面|当前视频系列|下一步|INDEXED FOOTAGE|服务端确认的资料/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "新建视频系列" }));
    await user.type(screen.getByRole("textbox", { name: "视频系列名称" }), "MyGO");
    await user.type(screen.getByRole("textbox", { name: "背景资料（选填）" }), "灯与爱音是乐队成员。这里记录人物关系与剧情背景。");
    await user.click(screen.getByRole("button", { name: "创建视频系列" }));

    expect(await screen.findByText("灯与爱音是乐队成员。这里记录人物关系与剧情背景。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "MyGO" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("dialog", { name: "新建视频系列" })).not.toBeInTheDocument();
    const creation = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/series/create"));
    expect(JSON.parse(String(creation?.[1]?.body))).toEqual({
      workspaceId: "local-default",
      name: "MyGO",
      description: null,
      importantExtraData: { format: "markdown", content: "灯与爱音是乐队成员。这里记录人物关系与剧情背景。" },
    });
  });

  it("edits an existing Series through the update API and displays the returned detail", async () => {
    const { fetchMock } = renderApp();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("link", { name: "资料库" })[0]!);
    await user.click(await screen.findByRole("button", { name: "编辑背景" }));
    await user.type(screen.getByRole("textbox", { name: "背景正文" }), "# 人物关系\n背景资料来自用户的补充。");
    await user.click(screen.getByRole("button", { name: "保存背景" }));
    expect(await screen.findByText("已保存")).toBeInTheDocument();
    expect(screen.getByText(/背景资料来自用户的补充/)).toBeInTheDocument();
    const update = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/series/update"));
    expect(JSON.parse(String(update?.[1]?.body))).toEqual({ workspaceId: "local-default", seriesId: apiSeries.seriesId, importantExtraData: { format: "markdown", content: "# 人物关系\n背景资料来自用户的补充。" } });
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/jobs/create"))).toBe(false);
    await user.click(screen.getByRole("button", { name: "编辑背景" }));
    expect(screen.getByRole("textbox", { name: "背景正文" })).toHaveValue("# 人物关系\n背景资料来自用户的补充。");
  });

  it("deletes a recovered upload from the library only after server confirmation and protects merging uploads", async () => {
    const merging = { ...pendingUpload, uploadSessionId: "merging", originalFilename: "merging.mp4", status: "COMPLETING" as const };
    const { fetchMock } = renderApp([{ ...apiSeries, pendingUploads: [pendingUpload, merging], pendingUploadCount: 2 }]);
    const original = fetchMock.getMockImplementation()!;
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes("/api/uploads/delete")) await pending;
      return original(input, init);
    });
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("link", { name: "资料库" })[0]!);
    const button = await screen.findByRole("button", { name: `删除上传 ${pendingUpload.originalFilename}` });
    expect(screen.queryByRole("button", { name: `删除上传 ${merging.originalFilename}` })).not.toBeInTheDocument();
    await user.click(button);
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("正在删除");
    expect(screen.getByText(pendingUpload.originalFilename)).toBeInTheDocument();
    await act(async () => release());
    await waitFor(() => expect(screen.queryByText(pendingUpload.originalFilename)).not.toBeInTheDocument());
    expect(screen.getByText(merging.originalFilename)).toBeInTheDocument();
    const deletion = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/uploads/delete"));
    expect(String(deletion?.[0])).toBe("/api/uploads/delete?uploadSessionId=upload%2Fsession&workspaceId=local-default");
    expect(deletion?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/jobs/delete"))).toBe(false);
  });

  it.each([503, 409])("keeps an upload visible with the API error and request identifier when deletion returns %s", async (status) => {
    const { fetchMock } = renderApp([{ ...apiSeries, pendingUploads: [pendingUpload], pendingUploadCount: 1 }]);
    const original = fetchMock.getMockImplementation()!;
    let rejected = false;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes("/api/uploads/delete") && !rejected) {
        rejected = true;
        return { ok: false, status, json: async () => ({ message: "暂时无法删除上传", requestId: "delete-request" }) } as Response;
      }
      return original(input, init);
    });
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("link", { name: "资料库" })[0]!);
    await user.click(await screen.findByRole("button", { name: `删除上传 ${pendingUpload.originalFilename}` }));
    expect(await screen.findByRole("alert")).toHaveTextContent("删除失败：暂时无法删除上传（请求编号：delete-request）");
    expect(screen.getByText(pendingUpload.originalFilename)).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/uploads/delete"))).toHaveLength(1);
    if (status === 503) {
      await user.click(screen.getByRole("button", { name: `删除上传 ${pendingUpload.originalFilename}` }));
      await waitFor(() => expect(screen.queryByText(pendingUpload.originalFilename)).not.toBeInTheDocument());
      expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/uploads/delete"))).toHaveLength(2);
    }
  });

  it("creates a Conversation without fixed scope and renders a real delta before durable completion", async () => {
    const { fetchMock, finishAnswer } = renderApp();
    const user = userEvent.setup();

    const composer = await screen.findByRole("textbox", {
      name: "向名场面检索助手提问",
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
    expect(String(creation?.[0])).toBe("/api/agent/conversations/create");
    expect(JSON.parse(String(creation?.[1]?.body))).toEqual({ workspaceId: "local-default" });
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/agent/sessions/"))).toBe(false);
    const sendCall = fetchMock.mock.calls.find(([input]) =>
      String(input).includes("/api/agent/messages/send?conversationId=conversation"),
    );
    expect(sendCall).toBeDefined();
    expect(new Headers(sendCall?.[1]?.headers).get("Accept")).toBe("text/event-stream");
    expect(JSON.parse(String(sendCall?.[1]?.body))).toEqual({
      workspaceId: "local-default",
      content: "雨天门口",
      seriesIds: [],
      topK: 10, reference: null,
    });
  });

  it("allows multi-selection and keeps a running answer and its scope when the next selection changes", async () => {
    const secondSeries = { ...apiSeries, seriesId: "second-series", name: "第二部动画" };
    const unreadySeries = { ...apiSeries, seriesId: "unready-series", name: "解析中的动画", status: "INDEXING" as const };
    const { fetchMock, finishAnswer } = renderApp([apiSeries, secondSeries, unreadySeries]);
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("选择检索视频系列"));
    const firstChoice = await screen.findByRole("checkbox", { name: new RegExp(apiSeries.name) });
    const secondChoice = screen.getByRole("checkbox", { name: /第二部动画/ });
    expect(screen.getByRole("checkbox", { name: /解析中的动画/ })).toBeDisabled();
    await user.click(firstChoice);
    await user.click(secondChoice);
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("已选 2 个系列");
    await user.type(screen.getByRole("textbox", { name: "向名场面检索助手提问" }), "雨天门口");
    await waitFor(() => expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("这是 Agent Chat")).toBeInTheDocument();
    await user.click(firstChoice);
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("已选 1 个系列");
    expect(screen.getByText(`检索范围：${apiSeries.name}、第二部动画`)).toBeInTheDocument();
    act(finishAnswer);
    expect(await screen.findByText("这是 Agent Chat 保存的完整回答。")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/conversations/create"))).toHaveLength(1);
    const sendCall = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/agent/messages/send"));
    expect(JSON.parse(String(sendCall?.[1]?.body)).seriesIds).toEqual([apiSeries.seriesId, secondSeries.seriesId]);
    expect(sendCall?.[1]?.signal?.aborted).toBe(false);
    await user.click(secondChoice);
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("全部视频系列");
    await user.type(screen.getByRole("textbox", { name: "向名场面检索助手提问" }), "雨天门口");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/messages/send"))).toHaveLength(2));
    const calls = fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/messages/send"));
    expect(JSON.parse(String(calls[1]?.[1]?.body)).seriesIds).toEqual([]);
    act(finishAnswer);
  });

  it("allows deselecting an animation that became unready and preserves unsent text when Enter is blocked", async () => {
    const secondSeries = { ...apiSeries, seriesId: "second-series", name: "第二部动画" };
    const { fetchMock, queryClient, finishAnswer } = renderApp([apiSeries, secondSeries]);
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("选择检索视频系列"));
    const firstChoice = await screen.findByRole("checkbox", { name: new RegExp(apiSeries.name) });
    await user.click(firstChoice);
    const composer = screen.getByRole("textbox", { name: "向名场面检索助手提问" });
    await user.type(composer, "雨天门口");
    await waitFor(() => expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled());
    act(() => queryClient.setQueryData(["series"], [{ ...apiSeries, status: "INDEXING" }, secondSeries]));
    await waitFor(() => expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled());
    expect(firstChoice).toBeChecked();
    expect(firstChoice).toBeEnabled();
    expect(screen.getByRole("alert")).toHaveTextContent("所选视频系列暂不可检索");
    await user.click(composer);
    await user.keyboard("{Enter}");
    expect(composer).toHaveValue("雨天门口");
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/agent/messages/send"))).toBe(false);
    await user.click(firstChoice);
    expect(firstChoice).not.toBeChecked();
    expect(firstChoice).toBeDisabled();
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("全部视频系列");
    expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled();
    await user.click(composer);
    await user.keyboard("{Enter}");
    expect(await screen.findByText("这是 Agent Chat")).toBeInTheDocument();
    act(finishAnswer);
  });

  it("does not send or discard a question on Enter when no animation is ready", async () => {
    const { fetchMock } = renderApp([{ ...apiSeries, status: "INDEXING" }]);
    const user = userEvent.setup();
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/agent/messages/list"))).toBe(true));
    const composer = screen.getByRole("textbox", { name: "向名场面检索助手提问" });
    await user.type(composer, "雨天门口{Enter}");
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    expect(composer).toHaveValue("雨天门口");
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/agent/messages/send"))).toBe(false);
  });
});
