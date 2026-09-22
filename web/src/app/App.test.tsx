import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentConversationView, AgentGlobalSettings, AgentMessageOptions, AgentMessageView, SeriesCreateRequest, SeriesUpdateRequest, SeriesView, UploadSessionView, VideoEpisodeUpdateRequest, VideoSummary } from "../api/contracts";
import { App } from "./App";
import { ThemeProvider } from "./theme";
import { WorkspaceProvider } from "./workspace";

const apiSeries: SeriesView = {
  seriesId: "series-from-api",
  workspaceId: "local-default",
  name: "接口返回的系列",
  description: "这条资料来自 Series API。",
  transcriptLanguageHint: null,
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

const initialMessageOptions: AgentMessageOptions = {
  topK: { defaultValue: 10, minimum: 1, maximum: 20 },
  maxLoopTurns: { defaultValue: 100, minimum: 1, maximum: 100 },
  maxModelCallsPerTurn: { defaultValue: 6, minimum: 1, maximum: 12 },
  maxToolCallsPerTurn: { defaultValue: 24, minimum: 1, maximum: 48 },
};

const initialGlobalSettings: AgentGlobalSettings = {
  revision: "rev-1",
  restartRequiredFields: ["maximum_concurrent_kicks"],
  activeValues: { maximum_concurrent_kicks: 4 },
  values: {
    history_message_limit: 12,
    default_top_k: 10,
    maximum_top_k: 20,
    max_loop_turns: 3,
    max_model_calls_per_turn: 6,
    maximum_model_calls_per_turn: 12,
    max_tool_calls_per_turn: 24,
    maximum_tool_calls_per_turn: 48,
    limits_enabled: false,
    query_reasoning_effort: "low",
    evaluator_reasoning_effort: "low",
    chat_reasoning_effort: "low",
    max_output_tokens: 4096,
    max_prompt_chars: 200000,
    context_window_tokens: 64000,
    context_safety_margin_tokens: 4096,
    auto_compact_tokens: 48000,
    compact_target_tokens: 24000,
    max_tool_result_bytes: 16384,
    max_read_chars_per_call: 16384,
    max_catalog_items_per_page: 20,
    stage_timeout_seconds: 120,
    evaluator_stage_timeout_seconds: 180,
    execution_timeout_seconds: 600,
    maximum_concurrent_kicks: 4,
  },
};

const videoWithoutProgress: VideoSummary = {
  videoId: "video-without-progress",
  workspaceId: "local-default",
  seriesId: apiSeries.seriesId,
  episodeOrder: 1,
  uploadSequence: 1,
  originalFilename: "calculating-progress.mp4",
  totalBytes: 1024,
  durationMs: 20_000,
  uploadStatus: "COMPLETED",
  uploadProgress: 100,
  parseStatus: "RUNNING",
  parseProgress: null,
  indexStatus: "NOT_READY",
  playbackStatus: "READY",
  playbackUrl: "/api/videos/content?videoId=video-without-progress",
  playbackUrlExpiresAt: null,
  thumbnail: null,
  latestJobId: "job-without-ranges",
  lastError: null,
};

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response;
}

function renderApp(
  availableSeries: SeriesView[] = [apiSeries],
  options: { failAgentSend?: boolean; failConversationList?: boolean; failInitialConversationCreation?: boolean; failGlobalSettingsSave?: boolean } = {},
) {
  let createdSeries: SeriesView | null = null;
  let finishAnswer: (() => void) | undefined;
  let messageSends = 0;
  let conversationCreates = 0;
  let messageOptions = initialMessageOptions;
  let globalSettings = initialGlobalSettings;
  const now = Date.parse("2026-09-15T00:00:00Z");
  const conversation: AgentConversationView = { conversationId: "conversation", workspaceId: "local-default", title: "Test", messageCount: 0, lastMessagePreview: null, lastMessageAt: null, createdAt: now, updatedAt: now };
  let conversations: AgentConversationView[] = [conversation];
  const conversationMessages = new Map<string, AgentMessageView[]>();
  const assistantMessage: AgentMessageView = {
    messageId: "assistant", conversationId: "conversation", seriesIds: [apiSeries.seriesId], sequenceNo: 1, role: "ASSISTANT", status: "PROCESSING", phase: "QUEUED", version: 1, reference: null, content: null,
    modelName: null, replyToMessageId: "user", retrievalQuery: null, searchResult: null,
    renderBlocks: [], timing: null, lastError: null, createdAt: now, updatedAt: now,
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
    if (url.includes("/api/videos/update_episode")) {
      const request = JSON.parse(String(_init?.body)) as VideoEpisodeUpdateRequest;
      let updated: VideoSummary | undefined;
      availableSeries = availableSeries.map((series) => ({
        ...series,
        videos: series.videos.map((video) => {
          if (video.videoId !== request.videoId) return video;
          updated = { ...video, episodeOrder: request.episodeOrder };
          return updated;
        }),
      }));
      if (!updated) throw new Error(`Unknown video in test: ${request.videoId}`);
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
    if (url.includes("/api/agent/conversations/create")) {
      conversationCreates += 1;
      if (conversationCreates === 1 && options.failInitialConversationCreation) {
        return new Response(JSON.stringify({ code: "UNAVAILABLE", message: "暂时无法恢复当前对话" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (conversationCreates === 1) return jsonResponse(conversation);
      const created = { ...conversation, conversationId: `conversation-${conversationCreates}`, title: "新对话", createdAt: now + conversationCreates, updatedAt: now + conversationCreates };
      conversations = [created, ...conversations];
      return jsonResponse(created);
    }
    if (url.includes("/api/agent/conversations/list")) {
      if (options.failConversationList) {
        return new Response(JSON.stringify({ code: "UNAVAILABLE", message: "暂时无法读取对话列表" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      return jsonResponse({ items: conversations, total: conversations.length, hasMore: false, nextCursor: null });
    }
    if (url.includes("/api/agent/conversations/get")) {
      const conversationId = new URL(url, "http://localhost").searchParams.get("conversationId");
      return jsonResponse(conversations.find((item) => item.conversationId === conversationId) ?? conversation);
    }
    if (url.includes("/api/agent/conversations/delete")) {
      const conversationId = new URL(url, "http://localhost").searchParams.get("conversationId");
      const deleted = conversations.find((item) => item.conversationId === conversationId) ?? conversation;
      conversations = conversations.filter((item) => item.conversationId !== conversationId);
      conversationMessages.delete(String(conversationId));
      return jsonResponse(deleted);
    }
    if (url.includes("/api/agent/messages/options")) {
      return jsonResponse(messageOptions);
    }
    if (url.includes("/api/agent/global-settings")) {
      if (_init?.method !== "PATCH") return jsonResponse(globalSettings);
      if (options.failGlobalSettingsSave) {
        return new Response(JSON.stringify({ code: "CONFIG_CONFLICT", message: "配置已变化" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        });
      }
      const patch = JSON.parse(String(_init.body)) as { values: Partial<AgentGlobalSettings["values"]>; revision: string };
      globalSettings = { ...globalSettings, revision: "rev-2", values: { ...globalSettings.values, ...patch.values } };
      return jsonResponse(globalSettings);
    }
    if (url.includes("/api/agent/messages/list")) {
      const conversationId = new URL(url, "http://localhost").searchParams.get("conversationId")!;
      const items = conversationMessages.get(conversationId) ?? [];
      return jsonResponse({ items, total: items.length, hasMore: false, nextCursor: null });
    }
    if (url.includes("/api/agent/messages/send")) {
      if (options.failAgentSend) {
        return new Response(JSON.stringify({ code: "UNAVAILABLE", message: "模型暂时不可用" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      const conversationId = new URL(url, "http://localhost").searchParams.get("conversationId")!;
      const request = JSON.parse(String(_init?.body)) as { content: string; seriesIds: string[] };
      const requestedIds = request.seriesIds;
      const seriesIds = requestedIds.length ? requestedIds : availableSeries.filter((item) => item.status === "READY").map((item) => item.seriesId);
      const index = messageSends++;
      const existingMessages = conversationMessages.get(conversationId) ?? [];
      const userSnapshot: AgentMessageView = { ...userMessage, conversationId, messageId: `user-${index}`, sequenceNo: existingMessages.length, role: "USER", status: "COMPLETED", content: request.content, seriesIds };
      const assistantSnapshot: AgentMessageView = { ...assistantMessage, conversationId, messageId: `assistant-${index}`, replyToMessageId: userSnapshot.messageId, sequenceNo: existingMessages.length + 1, seriesIds };
      conversationMessages.set(conversationId, [...existingMessages, userSnapshot, assistantSnapshot]);
      conversations = conversations.map((item) => item.conversationId === conversationId ? {
        ...item,
        title: item.messageCount === 0 ? request.content.slice(0, 40) : item.title,
        messageCount: item.messageCount + 2,
        lastMessagePreview: request.content,
        lastMessageAt: now + index + 1,
        updatedAt: now + index + 1,
      } : item);
      const body = new ReadableStream<Uint8Array>({ start(controller) {
        const emit = (type: string, payload: unknown) => controller.enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify(payload)}\n\n`));
        const base = { occurredAt: now, conversationId, messageId: assistantSnapshot.messageId };
        emit("answer.started", { ...base, sequence: 1, userMessage: userSnapshot, assistantMessage: assistantSnapshot });
        emit("answer.evidence", { ...base, sequence: 2, searchResult: { query: "雨天门口", seriesIds, searchedVideoIds: [], partial: false, warnings: [], hits: [] } });
        emit("answer.delta", { ...base, sequence: 3, delta: "这是 Agent Chat" });
        finishAnswer = () => {
          const completed: AgentMessageView = { ...assistantSnapshot, version: 2, phase: null, status: "COMPLETED", content: "这是 Agent Chat 保存的完整回答。" };
          conversationMessages.set(conversationId, [...existingMessages, userSnapshot, completed]);
          emit("answer.delta", { ...base, sequence: 4, delta: " 保存的完整回答。" });
          emit("answer.completed", { ...base, sequence: 5, userMessage: userSnapshot, assistantMessage: completed });
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
  return {
    ...view,
    fetchMock,
    queryClient,
    finishAnswer: () => finishAnswer?.(),
    setMessageOptions: (next: AgentMessageOptions) => { messageOptions = next; },
  };
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

    expect(screen.getByRole("heading", { name: "anivag 助手", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("anivag 助手")).toBeInTheDocument();
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

  it("saves global Agent settings and shows saved versus active restart values", async () => {
    const { fetchMock } = renderApp();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("link", { name: /设置/ })[0]!);
    const concurrency = await screen.findByRole("spinbutton", { name: /并行任务上限/ });
    await waitFor(() => expect(concurrency).toBeEnabled());
    expect(concurrency).toHaveValue(4);
    fireEvent.input(concurrency, { target: { value: "6" } });
    expect(screen.getByRole("button", { name: "保存全局配置" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "保存全局配置" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).includes("/api/agent/global-settings") && init?.method === "PATCH",
    )).toBe(true));
    const saveCall = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes("/api/agent/global-settings") && init?.method === "PATCH",
    );
    expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
      revision: "rev-1",
      values: { maximum_concurrent_kicks: 6 },
    });
    expect(await screen.findByText(/并行任务上限已保存为/)).toHaveTextContent("当前服务仍使用 4");
    expect(screen.getAllByText(/重启服务后生效/)).toHaveLength(2);
  });

  it("saves the read tool character limit from global settings", async () => {
    const { fetchMock } = renderApp();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("link", { name: /设置/ })[0]!);
    await user.click(screen.getByText("工具输出"));
    expect(screen.getByText(/read 工具按片段内容的字符范围读取，不按行读取/)).toBeInTheDocument();
    const readLimit = await screen.findByRole("spinbutton", { name: /read 单次最多读取字符数/ });
    await waitFor(() => expect(readLimit).toBeEnabled());
    expect(readLimit).toHaveValue(16384);
    fireEvent.input(readLimit, { target: { value: "4096" } });
    await user.click(screen.getByRole("button", { name: "保存全局配置" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).includes("/api/agent/global-settings") && init?.method === "PATCH",
    )).toBe(true));
    const saveCall = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes("/api/agent/global-settings") && init?.method === "PATCH",
    );
    expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
      revision: "rev-1",
      values: { max_read_chars_per_call: 4096 },
    });
    expect(await screen.findByText("已保存。新的请求会使用更新后的全局配置。")).toBeInTheDocument();
  });

  it("saves independent thinking modes for Query, Evaluator, and Chat", async () => {
    const { fetchMock } = renderApp();
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("link", { name: /设置/ })[0]!);
    await screen.findByRole("spinbutton", { name: /并行任务上限/ });
    await user.click(screen.getByText("模型思考"));
    const query = screen.getByRole("combobox", { name: /Query 检索/ });
    const evaluator = screen.getByRole("combobox", { name: /Evaluator 审核/ });
    const chat = screen.getByRole("combobox", { name: /Chat 回答/ });
    expect(query).toHaveValue("low");
    expect(evaluator).toHaveValue("low");
    expect(chat).toHaveValue("low");
    await user.selectOptions(query, "high");
    await user.selectOptions(evaluator, "max");
    await user.selectOptions(chat, "none");
    await user.click(screen.getByRole("button", { name: "保存全局配置" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) =>
      String(input).includes("/api/agent/global-settings") && init?.method === "PATCH",
    )).toBe(true));
    const saveCall = fetchMock.mock.calls.find(([input, init]) =>
      String(input).includes("/api/agent/global-settings") && init?.method === "PATCH",
    );
    expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
      revision: "rev-1",
      values: {
        query_reasoning_effort: "high",
        evaluator_reasoning_effort: "max",
        chat_reasoning_effort: "none",
      },
    });
    expect(await screen.findByText("已保存。新的请求会使用更新后的全局配置。")).toBeInTheDocument();
  });

  it("preserves form edits and asks for a reload after a YAML revision conflict", async () => {
    renderApp([apiSeries], { failGlobalSettingsSave: true });
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("link", { name: /设置/ })[0]!);
    const timeout = await screen.findByRole("spinbutton", { name: /审核阶段超时/ });
    await waitFor(() => expect(timeout).toBeEnabled());
    fireEvent.input(timeout, { target: { value: "240" } });
    expect(screen.getByRole("button", { name: "保存全局配置" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "保存全局配置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请重新读取后再保存");
    expect(timeout).toHaveValue(240);
  });

  it("collapses the desktop navigation without losing the draft and restores the preference", async () => {
    const { unmount } = renderApp();
    const user = userEvent.setup();
    const composer = screen.getByRole("textbox", { name: "向 anivag 助手提问" });
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
      transcriptLanguageHint: null,
      importantExtraData: { format: "markdown", content: "灯与爱音是乐队成员。这里记录人物关系与剧情背景。" },
    });
  });

  it("shows unavailable checkpoint progress without falling back to a stale percent", async () => {
    renderApp([{
      ...apiSeries,
      status: "INDEXING",
      videoCount: 1,
      readyVideoCount: 0,
      videos: [videoWithoutProgress],
    }]);
    const user = userEvent.setup();

    await user.click(screen.getAllByRole("link", { name: "资料库" })[0]!);

    expect(await screen.findByText(videoWithoutProgress.originalFilename)).toBeInTheDocument();
    const unavailable = screen.getByText("—");
    expect(unavailable).toBeInTheDocument();
    expect(unavailable.closest("div")?.querySelector("em")).toBeNull();
    expect(screen.queryByText("34%")).not.toBeInTheDocument();
  });

  it("edits a video episode in place and applies the server result without reparsing", async () => {
    const series = {
      ...apiSeries,
      status: "INDEXING" as const,
      videoCount: 1,
      readyVideoCount: 0,
      videos: [videoWithoutProgress],
    };
    const { fetchMock } = renderApp([series]);
    const original = fetchMock.getMockImplementation()!;
    let release!: () => void;
    const pending = new Promise<void>((resolve) => { release = resolve; });
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes("/api/videos/update_episode")) await pending;
      return original(input, init);
    });
    const user = userEvent.setup();

    await user.click(screen.getAllByRole("link", { name: "资料库" })[0]!);
    const edit = await screen.findByRole("button", { name: `编辑 ${videoWithoutProgress.originalFilename} 集数` });
    await user.click(edit);
    const input = screen.getByRole("spinbutton", { name: `${videoWithoutProgress.originalFilename} 集数` });
    const save = screen.getByRole("button", { name: `保存 ${videoWithoutProgress.originalFilename} 集数` });
    expect(input).toHaveValue(1);
    expect(save).toBeDisabled();

    await user.clear(input);
    await user.type(input, "0");
    expect(save).toBeDisabled();
    await user.clear(input);
    await user.type(input, "7");
    await user.click(save);

    expect(screen.getByRole("button", { name: `正在保存 ${videoWithoutProgress.originalFilename} 集数` })).toBeDisabled();
    act(release);
    expect(await screen.findByText("EP 07")).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton", { name: `${videoWithoutProgress.originalFilename} 集数` })).not.toBeInTheDocument();

    const update = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/videos/update_episode"));
    expect(String(update?.[0])).toBe("/api/videos/update_episode");
    expect(JSON.parse(String(update?.[1]?.body))).toEqual({
      workspaceId: "local-default",
      videoId: videoWithoutProgress.videoId,
      episodeOrder: 7,
    });
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/uploads/reparse"))).toBe(false);
  });

  it("keeps the episode draft open when the server rejects the change", async () => {
    const { fetchMock } = renderApp([{
      ...apiSeries,
      videoCount: 1,
      videos: [videoWithoutProgress],
    }]);
    const original = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes("/api/videos/update_episode")) {
        return new Response(JSON.stringify({ code: "CONFLICT", message: "这个集数已被占用" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        });
      }
      return original(input, init);
    });
    const user = userEvent.setup();

    await user.click(screen.getAllByRole("link", { name: "资料库" })[0]!);
    await user.click(await screen.findByRole("button", { name: `编辑 ${videoWithoutProgress.originalFilename} 集数` }));
    const input = screen.getByRole("spinbutton", { name: `${videoWithoutProgress.originalFilename} 集数` });
    await user.clear(input);
    await user.type(input, "2");
    await user.click(screen.getByRole("button", { name: `保存 ${videoWithoutProgress.originalFilename} 集数` }));

    expect(await screen.findByRole("alert")).toHaveTextContent("保存失败：这个集数已被占用");
    expect(input).toHaveValue(2);
    await user.click(screen.getByRole("button", { name: `取消编辑 ${videoWithoutProgress.originalFilename} 集数` }));
    expect(screen.getByText("EP 01")).toBeInTheDocument();
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

    expect(await screen.findByText("服务端默认")).toBeInTheDocument();
    const composer = await screen.findByRole("textbox", {
      name: "向 anivag 助手提问",
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
    expect(await screen.findByText("回答已完成")).toHaveAttribute("role", "status");
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
      reference: null,
    });
  });

  it("sends the answer settings selected beside the Chat composer", async () => {
    localStorage.setItem("anivag-agent-message-options:local-default", JSON.stringify({
      serverOptions: initialMessageOptions,
      selection: {
        topK: 5,
        maxLoopTurns: 0,
        maxModelCallsPerTurn: 9,
        maxToolCallsPerTurn: 36,
      },
    }));
    const { fetchMock, finishAnswer } = renderApp();
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("配置本次回答"));
    expect(screen.getByRole("button", { name: "自动" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("slider", { name: "模型调用上限" })).not.toBeInTheDocument();
    expect(screen.queryByRole("slider", { name: "工具调用上限" })).not.toBeInTheDocument();
    expect(screen.getByText("每次检索最多返回多少段候选片段")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("slider", { name: "单次检索候选片段数量" }), { target: { value: "7" } });
    await user.click(screen.getByRole("button", { name: "自定义" }));
    expect(screen.getByRole("slider", { name: "最多运行轮次" })).toHaveAttribute("max", "100");
    expect(screen.getByRole("slider", { name: "最多运行轮次" })).toHaveValue("100");
    fireEvent.change(screen.getByRole("slider", { name: "最多运行轮次" }), { target: { value: "37" } });
    await user.click(screen.getByRole("button", { name: "自动" }));
    await user.click(screen.getByRole("button", { name: "自定义" }));
    expect(screen.getByRole("slider", { name: "最多运行轮次" })).toHaveValue("37");
    fireEvent.change(screen.getByRole("slider", { name: "最多运行轮次" }), { target: { value: "100" } });
    await user.type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    const sendCall = fetchMock.mock.calls.find(([input]) =>
      String(input).includes("/api/agent/messages/send?conversationId=conversation"),
    );
    expect(JSON.parse(String(sendCall?.[1]?.body))).toMatchObject({
      topK: 7,
      maxLoopTurns: 100,
      maxModelCallsPerTurn: 9,
      maxToolCallsPerTurn: 36,
    });
    expect(JSON.parse(localStorage.getItem("anivag-agent-message-options:local-default")!)).toEqual({
      overrides: {
        topK: 7,
        maxLoopTurns: 100,
        maxModelCallsPerTurn: 9,
        maxToolCallsPerTurn: 36,
      },
    });
    act(finishAnswer);
  });

  it("keeps explicit overrides after a YAML change and omits untouched defaults", async () => {
    localStorage.setItem("anivag-agent-message-options:local-default", JSON.stringify({
      topK: 5,
      maxLoopTurns: 100,
      maxModelCallsPerTurn: 9,
      maxToolCallsPerTurn: 36,
    }));
    const { fetchMock, setMessageOptions, finishAnswer } = renderApp();
    expect(await screen.findByText("服务端默认")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByLabelText("配置本次回答"));
    fireEvent.change(screen.getByRole("slider", { name: "单次检索候选片段数量" }), { target: { value: "7" } });
    expect(screen.getByText("每次检索 7 段")).toBeInTheDocument();
    act(() => window.dispatchEvent(new Event("focus")));
    expect(screen.getByText("每次检索 7 段")).toBeInTheDocument();

    setMessageOptions({
      ...initialMessageOptions,
      topK: { ...initialMessageOptions.topK, defaultValue: 8 },
    });
    act(() => window.dispatchEvent(new Event("focus")));
    expect(await screen.findByText("每次检索 7 段")).toBeInTheDocument();
    await userEvent.setup().type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await userEvent.setup().click(screen.getByRole("button", { name: "发送问题" }));
    const sendCall = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/agent/messages/send?conversationId=conversation"));
    expect(JSON.parse(String(sendCall?.[1]?.body))).toMatchObject({ topK: 7 });
    expect(JSON.parse(String(sendCall?.[1]?.body))).not.toHaveProperty("maxModelCallsPerTurn");
    act(finishAnswer);
  });

  it("uses a refreshed server default when Chat has no override", async () => {
    const { fetchMock, setMessageOptions, finishAnswer } = renderApp();
    expect(await screen.findByText("服务端默认")).toBeInTheDocument();
    setMessageOptions({
      ...initialMessageOptions,
      topK: { ...initialMessageOptions.topK, defaultValue: 8 },
    });
    act(() => window.dispatchEvent(new Event("focus")));
    await userEvent.setup().click(screen.getByLabelText("配置本次回答"));
    expect(screen.getByRole("slider", { name: "单次检索候选片段数量" })).toHaveValue("8");
    await userEvent.setup().type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await userEvent.setup().click(screen.getByRole("button", { name: "发送问题" }));
    const sendCall = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/agent/messages/send?conversationId=conversation"));
    expect(JSON.parse(String(sendCall?.[1]?.body))).not.toHaveProperty("topK");
    expect(JSON.parse(localStorage.getItem("anivag-agent-message-options:local-default")!)).toEqual({ overrides: {} });
    act(finishAnswer);
  });

  it("keeps an in-flight Auto request unchanged when the next answer is set to custom turns", async () => {
    const { fetchMock, finishAnswer } = renderApp();
    const user = userEvent.setup();
    const composer = await screen.findByRole("textbox", { name: "向 anivag 助手提问" });
    await user.type(composer, "第一个问题");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("这是 Agent Chat")).toBeInTheDocument();

    await user.click(screen.getByLabelText("配置本次回答"));
    await user.click(screen.getByRole("button", { name: "自定义" }));
    fireEvent.change(screen.getByRole("slider", { name: "最多运行轮次" }), { target: { value: "42" } });
    const sendCalls = () => fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/messages/send?conversationId=conversation"));
    expect(JSON.parse(String(sendCalls()[0]?.[1]?.body))).not.toHaveProperty("maxLoopTurns");

    act(finishAnswer);
    await screen.findByText("这是 Agent Chat 保存的完整回答。");
    await user.type(composer, "第二个问题");
    await waitFor(() => expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    await waitFor(() => expect(sendCalls()).toHaveLength(2));
    expect(JSON.parse(String(sendCalls()[1]?.[1]?.body)).maxLoopTurns).toBe(42);
    act(finishAnswer);
  });

  it("announces an unfinished answer when sending fails before the stream starts", async () => {
    renderApp([apiSeries], { failAgentSend: true });
    const user = userEvent.setup();
    await user.type(await screen.findByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(await screen.findByText("模型暂时不可用")).toHaveAttribute("role", "alert");
    expect(await screen.findByText("回答未完成")).toHaveAttribute("role", "status");
    expect(screen.queryByText("回答已完成")).not.toBeInTheDocument();
  });

  it("creates, lists, and switches between Conversations without duplicating an empty one", async () => {
    const { fetchMock, finishAnswer } = renderApp();
    const user = userEvent.setup();
    const composer = await screen.findByRole("textbox", { name: "向 anivag 助手提问" });

    await user.click(screen.getByRole("button", { name: "新建对话" }));
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/conversations/create"))).toHaveLength(1);

    await user.type(composer, "雨天门口");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("这是 Agent Chat")).toBeInTheDocument();
    act(finishAnswer);
    expect(await screen.findByText("这是 Agent Chat 保存的完整回答。")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "新建对话" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/conversations/create"))).toHaveLength(2));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation-2"));
    expect(await screen.findByText("可以聊天，也可以询问视频内容或查找资料。")).toBeInTheDocument();

    await user.click(screen.getByLabelText("打开对话记录"));
    await user.click(await screen.findByRole("button", { name: /^打开对话 雨天门口，/ }));
    expect(await screen.findByText("这是 Agent Chat 保存的完整回答。")).toBeInTheDocument();
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation"));
  });

  it("reuses the recovered empty Conversation when the history list is unavailable", async () => {
    const { fetchMock } = renderApp([apiSeries], { failConversationList: true });
    const user = userEvent.setup();
    await screen.findByRole("textbox", { name: "向 anivag 助手提问" });
    await user.click(screen.getByRole("button", { name: "新建对话" }));

    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/conversations/create"))).toHaveLength(1);
    expect(screen.getByRole("textbox", { name: "向 anivag 助手提问" })).toHaveFocus();
  });

  it("loads Conversation history on mount even when automatic creation fails", async () => {
    renderApp([apiSeries], { failInitialConversationCreation: true });
    const user = userEvent.setup();
    expect(await screen.findByText("暂时无法恢复当前对话")).toBeInTheDocument();

    await user.click(screen.getByLabelText("打开对话记录"));
    await user.click(await screen.findByRole("button", { name: /^打开对话 Test，/ }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation"));
    expect(screen.getByRole("textbox", { name: "向 anivag 助手提问" })).toBeEnabled();
  });

  it("focuses the readable error when an explicit Conversation switch fails", async () => {
    const { fetchMock } = renderApp([apiSeries], { failInitialConversationCreation: true });
    const user = userEvent.setup();
    await screen.findByText("暂时无法恢复当前对话");
    const original = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes("/api/agent/conversations/get")) {
        return new Response(JSON.stringify({ code: "UNAVAILABLE", message: "无法打开选中的对话" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      return original(input, init);
    });

    await user.click(screen.getByLabelText("打开对话记录"));
    await user.click(await screen.findByRole("button", { name: /^打开对话 Test，/ }));
    const failure = await screen.findByText("无法打开选中的对话");
    await waitFor(() => expect(failure).toHaveFocus());
    expect(screen.getByRole("status")).toHaveTextContent("打开对话失败：无法打开选中的对话");
  });

  it("closes history accessibly and restores focus when cancelling deletion", async () => {
    renderApp();
    const user = userEvent.setup();
    const history = await screen.findByLabelText("打开对话记录");
    await user.click(history);
    await user.click(await screen.findByRole("button", { name: /^删除对话 Test，/ }));
    const confirmation = screen.getByRole("button", { name: /^确认删除对话 Test，/ });
    const cancellation = screen.getByRole("button", { name: "取消" });
    expect(cancellation).toHaveFocus();
    expect(confirmation).toHaveAttribute("aria-describedby");
    expect(document.getElementById(confirmation.getAttribute("aria-describedby")!)).toHaveTextContent("全部消息都无法恢复");

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("button", { name: /^确认删除对话 Test，/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^删除对话 Test，/ })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(history).toHaveFocus();

    await user.click(history);
    await user.click(screen.getByRole("button", { name: "关闭对话记录" }));
    expect(history).toHaveFocus();
  });

  it("keeps current deletion disabled while an answer is running", async () => {
    const { finishAnswer } = renderApp();
    const user = userEvent.setup();
    await user.type(await screen.findByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("这是 Agent Chat")).toBeInTheDocument();

    await user.click(screen.getByLabelText("打开对话记录"));
    const deletion = await screen.findByRole("button", { name: /^删除对话 雨天门口，/ });
    expect(deletion).toBeDisabled();
    expect(deletion).toHaveAttribute("title", "当前回答进行中，完成后才能删除");
    expect(screen.getByText("回答完成后可删除")).toBeInTheDocument();
    act(finishAnswer);
    await waitFor(() => expect(deletion).toBeEnabled());
  });

  it("locks deletion while a new Conversation request is in flight", async () => {
    const { fetchMock, finishAnswer } = renderApp();
    const user = userEvent.setup();
    await user.type(await screen.findByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    await screen.findByText("这是 Agent Chat");
    act(finishAnswer);
    await screen.findByText("这是 Agent Chat 保存的完整回答。");

    const original = fetchMock.getMockImplementation()!;
    let releaseCreation!: () => void;
    let markCreationStarted!: () => void;
    const creationRelease = new Promise<void>((resolve) => { releaseCreation = resolve; });
    const creationStarted = new Promise<void>((resolve) => { markCreationStarted = resolve; });
    let holdCreation = true;
    fetchMock.mockImplementation(async (input, init) => {
      if (holdCreation && String(input).includes("/api/agent/conversations/create")) {
        holdCreation = false;
        const response = await original(input, init);
        markCreationStarted();
        await creationRelease;
        return response;
      }
      return original(input, init);
    });

    await user.click(screen.getByRole("button", { name: "新建对话" }));
    await creationStarted;
    await user.click(screen.getByLabelText("打开对话记录"));
    const deletion = await screen.findByRole("button", { name: /^删除对话 雨天门口，/ });
    expect(deletion).toBeDisabled();
    expect(deletion).toHaveAttribute("title", "请等待当前对话操作完成");
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/agent/conversations/delete"))).toBe(false);

    act(releaseCreation);
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation-2"));
  });

  it("locks every deletion target while an explicitly selected Conversation is loading", async () => {
    const { fetchMock, finishAnswer } = renderApp();
    const user = userEvent.setup();
    await user.type(await screen.findByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    await screen.findByText("这是 Agent Chat");
    act(finishAnswer);
    await screen.findByText("这是 Agent Chat 保存的完整回答。");
    await user.click(screen.getByRole("button", { name: "新建对话" }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation-2"));

    const original = fetchMock.getMockImplementation()!;
    let releaseSelection!: () => void;
    let markSelectionStarted!: () => void;
    const selectionRelease = new Promise<void>((resolve) => { releaseSelection = resolve; });
    const selectionStarted = new Promise<void>((resolve) => { markSelectionStarted = resolve; });
    let holdSelection = true;
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (holdSelection && url.includes("/api/agent/conversations/get") && new URL(url, "http://localhost").searchParams.get("conversationId") === "conversation") {
        holdSelection = false;
        markSelectionStarted();
        await selectionRelease;
      }
      return original(input, init);
    });

    await user.click(screen.getByLabelText("打开对话记录"));
    await user.click(await screen.findByRole("button", { name: /^打开对话 雨天门口，/ }));
    await selectionStarted;
    await user.click(screen.getByLabelText("打开对话记录"));
    expect(screen.getByRole("button", { name: /^删除对话 雨天门口，/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^删除对话 新对话，/ })).toBeDisabled();

    act(releaseSelection);
    await waitFor(() => expect(screen.getByText("这是 Agent Chat 保存的完整回答。")).toBeInTheDocument());
  });

  it("deletes only after confirmation and replaces the final active Conversation", async () => {
    const { fetchMock, finishAnswer } = renderApp();
    const user = userEvent.setup();
    await user.type(await screen.findByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("这是 Agent Chat")).toBeInTheDocument();
    act(finishAnswer);
    expect(await screen.findByText("这是 Agent Chat 保存的完整回答。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "新建对话" }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation-2"));

    await user.click(screen.getByLabelText("打开对话记录"));
    await user.click(await screen.findByRole("button", { name: /^删除对话 雨天门口，/ }));
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/agent/conversations/delete"))).toBe(false);
    await user.click(screen.getByRole("button", { name: /^确认删除对话 雨天门口，/ }));
    await waitFor(() => expect(screen.queryByRole("button", { name: /^删除对话 雨天门口，/ })).not.toBeInTheDocument());
    expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation-2");

    await user.click(screen.getByRole("button", { name: /^删除对话 新对话，/ }));
    await user.click(screen.getByRole("button", { name: /^确认删除对话 新对话，/ }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/conversations/create"))).toHaveLength(3));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation-3"));
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/conversations/delete"))).toHaveLength(2);
  });

  it("does not restore a deleted Conversation from an older list response", async () => {
    const { fetchMock, finishAnswer } = renderApp();
    const original = fetchMock.getMockImplementation()!;
    let holdNextList = false;
    let releaseList!: () => void;
    let markListStarted!: () => void;
    const listRelease = new Promise<void>((resolve) => { releaseList = resolve; });
    const listStarted = new Promise<void>((resolve) => { markListStarted = resolve; });
    fetchMock.mockImplementation(async (input, init) => {
      if (holdNextList && String(input).includes("/api/agent/conversations/list")) {
        holdNextList = false;
        const staleResponse = await original(input, init);
        markListStarted();
        await listRelease;
        return staleResponse;
      }
      return original(input, init);
    });
    const user = userEvent.setup();
    await user.type(await screen.findByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("这是 Agent Chat")).toBeInTheDocument();
    act(finishAnswer);
    await screen.findByText("这是 Agent Chat 保存的完整回答。");
    await user.click(screen.getByRole("button", { name: "新建对话" }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation-2"));

    holdNextList = true;
    await user.click(screen.getByLabelText("打开对话记录"));
    await user.click(await screen.findByRole("button", { name: /^打开对话 雨天门口，/ }));
    await listStarted;
    await user.click(screen.getByLabelText("打开对话记录"));
    await user.click(screen.getByRole("button", { name: /^删除对话 雨天门口，/ }));
    await user.click(screen.getByRole("button", { name: /^确认删除对话 雨天门口，/ }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation-2"));
    act(releaseList);

    await waitFor(() => expect(screen.queryByRole("button", { name: /^删除对话 雨天门口，/ })).not.toBeInTheDocument());
  });

  it("keeps a Conversation visible when deletion is rejected", async () => {
    const { fetchMock } = renderApp();
    const original = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes("/api/agent/conversations/delete")) {
        return new Response(JSON.stringify({ code: "MESSAGE_IN_PROGRESS", message: "对话仍在处理消息", requestId: "conversation-delete" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        });
      }
      return original(input, init);
    });
    const user = userEvent.setup();
    await screen.findByRole("textbox", { name: "向 anivag 助手提问" });
    await user.click(screen.getByLabelText("打开对话记录"));
    await user.click(await screen.findByRole("button", { name: /^删除对话 Test，/ }));
    await user.click(screen.getByRole("button", { name: /^确认删除对话 Test，/ }));

    expect(await screen.findByText("删除对话失败：对话仍在处理消息（请求编号：conversation-delete）")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^删除对话 Test，/ })).toBeInTheDocument();
  });

  it("can create again when replacing the deleted final Conversation fails once", async () => {
    const { fetchMock } = renderApp();
    const original = fetchMock.getMockImplementation()!;
    let failReplacement = false;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).includes("/api/agent/conversations/delete")) {
        const response = await original(input, init);
        failReplacement = true;
        return response;
      }
      if (failReplacement && String(input).includes("/api/agent/conversations/create")) {
        failReplacement = false;
        return new Response(JSON.stringify({ code: "UNAVAILABLE", message: "暂时无法新建对话" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      return original(input, init);
    });
    const user = userEvent.setup();
    await screen.findByRole("textbox", { name: "向 anivag 助手提问" });
    await user.click(screen.getByLabelText("打开对话记录"));
    await user.click(await screen.findByRole("button", { name: /^删除对话 Test，/ }));
    await user.click(screen.getByRole("button", { name: /^确认删除对话 Test，/ }));
    expect(await screen.findByText("对话已删除，但新对话创建失败：暂时无法新建对话")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^删除对话 Test，/ })).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "不能发给已删除的对话");
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "新建对话" }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("anivag-agent-conversation:local-default")!).id).toBe("conversation-2"));
    expect(await screen.findByText("可以聊天，也可以询问视频内容或查找资料。")).toBeInTheDocument();
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
    await user.type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
    await waitFor(() => expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(await screen.findByText("这是 Agent Chat")).toBeInTheDocument();
    await user.click(firstChoice);
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("已选 1 个系列");
    expect(screen.getByText(`可用视频范围：${apiSeries.name}、第二部动画`)).toBeInTheDocument();
    act(finishAnswer);
    expect(await screen.findByText("这是 Agent Chat 保存的完整回答。")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/conversations/create"))).toHaveLength(1);
    const sendCall = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/agent/messages/send"));
    expect(JSON.parse(String(sendCall?.[1]?.body)).seriesIds).toEqual([apiSeries.seriesId, secondSeries.seriesId]);
    expect(sendCall?.[1]?.signal?.aborted).toBe(false);
    await user.click(secondChoice);
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("全部视频系列");
    await user.type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "雨天门口");
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
    const composer = screen.getByRole("textbox", { name: "向 anivag 助手提问" });
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

  it("lets the Supervisor answer without video scope when no animation is ready", async () => {
    const { fetchMock, finishAnswer } = renderApp([{ ...apiSeries, status: "INDEXING" }]);
    const user = userEvent.setup();
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/agent/messages/list"))).toBe(true));
    const composer = screen.getByRole("textbox", { name: "向 anivag 助手提问" });
    await user.type(composer, "雨天门口{Enter}");
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/agent/messages/send"))).toHaveLength(1));
    const sendCall = fetchMock.mock.calls.find(([input]) => String(input).includes("/api/agent/messages/send"));
    expect(JSON.parse(String(sendCall?.[1]?.body)).seriesIds).toEqual([]);
    expect(composer).toHaveValue("");
    act(finishAnswer);
  });
});
