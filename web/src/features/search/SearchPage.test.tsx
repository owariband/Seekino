import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentMessageView, AgentRenderBlock, RetrievalChannelWeights, SeriesSummary, VideoChunkSearchHit } from "../../api/contracts";
import * as api from "../../api/client";
import { SearchPage, type SearchPageHandle } from "./SearchPage";

const mocks = vi.hoisted(() => ({
  send: vi.fn(),
  stop: vi.fn(),
  useAgentConversation: vi.fn(),
  useWorkspace: vi.fn(),
  useAuth: vi.fn(),
}));

vi.mock("../../app/workspace", () => ({ useWorkspace: mocks.useWorkspace }));
vi.mock("./useAgentConversation", () => ({ useAgentConversation: mocks.useAgentConversation }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: mocks.useAuth }));

const now = Date.parse("2026-09-17T00:00:00Z");
const series: SeriesSummary = {
  visibility: "private", access: "member", publisher: null,
  seriesId: "series-1",
  tenantId: "local-default",
  name: "测试系列",
  description: null,
  status: "READY",
  videoCount: 1,
  readyVideoCount: 1,
  failedVideoCount: 0,
  pendingUploadCount: 0,
  cover: null,
  lastSearch: null,
  createdAt: now,
  updatedAt: now,
};

function searchHit(videoChunkId: string, rank: number, alt: string): VideoChunkSearchHit {
  const startSeconds = 12_000 + (rank - 1) * 10_000;
  return {
    candidate: {
      videoChunkId,
      rank,
      fusedRank: rank,
      fusedScore: 0.032,
      transcriptRank: rank,
      transcriptScore: 1,
      textRank: rank,
      textScore: 0.91,
      visionRank: rank,
      visionScore: 0.84,
    },
    selectionReason: {
      summary: `第 ${rank} 段同时命中对白和画面。`,
      matchedEvidence: [{ kind: "TRANSCRIPT", text: "我们在门口等你。", startSeconds, endSeconds: startSeconds + 2_000 }],
      limitations: [],
    },
    evidence: {
      seriesId: series.seriesId,
      tenantId: series.tenantId,
      seriesName: series.name,
      seriesStatus: "READY",
      videoId: "video-1",
      episodeOrder: 1,
      originalFilename: "episode-01.mp4",
      durationSeconds: 60_000,
      videoChunkId,
      sequenceNo: rank - 1,
      startSeconds,
      endSeconds: startSeconds + 3_000,
      analysisWindowStartSeconds: startSeconds - 2_000,
      analysisWindowEndSeconds: startSeconds + 6_000,
      transcript: [{ speaker: "角色甲", text: "我们在门口等你。", startSeconds, endSeconds: startSeconds + 2_000 }],
      baselineVisualCaption: "人物手持雨伞站在门口。",
      allTimeEvents: [],
      allTimeEventText: "人物手持雨伞站在门口。",
      videoCompact: `雨天候选片段 ${rank}`,
      contextBeforeCompact: "室外开始下雨。",
      contextAfterCompact: "另一人从走廊出现。",
      windowSummary: "一场发生在门口的等待。",
      modelProvider: "provider",
      modelName: "model",
      pipelineVersion: "m1",
      isFallback: false,
      parsedAt: now,
      playbackStatus: "READY",
      playbackUrl: "/api/videos/content?videoId=video-1&tenantId=local-default&variant=playback",
      playbackUrlExpiresAt: null,
      thumbnail: {
        imageId: `thumbnail-${rank}`,
        url: `/api/media/images/content?tenantId=local-default&imageId=thumbnail-${rank}`,
        expiresAt: null,
        atSeconds: startSeconds,
        alt,
      },
      keyframes: [],
    },
  };
}

function assistantMessage(content: string, hits: VideoChunkSearchHit[] = []): AgentMessageView {
  return {
    messageId: "assistant-1",
    conversationId: "conversation-1",
    seriesIds: [series.seriesId],
    sequenceNo: 1,
    role: "ASSISTANT",
    status: "COMPLETED",
    phase: null,
    version: 1,
    reference: null,
    content,
    attachments: [],
    modelName: "deepseek-flash",
    searchResult: hits.length ? {
      seriesIds: [series.seriesId],
      searchedVideoIds: ["video-1"],
      partial: false,
      warnings: [],
      hits,
    } : null,
    renderBlocks: [
      ...(content ? [{ type: "markdown" as const, markdown: content }] : []),
      ...hits.map((hit) => ({
        type: "video" as const,
        videoChunkId: hit.evidence.videoChunkId,
        startSeconds: hit.evidence.startSeconds,
        endSeconds: hit.evidence.endSeconds,
      })),
    ],
    timing: null,
    lastError: null,
    createdAt: now,
    updatedAt: now,
  };
}

function renderPage(
  message: AgentMessageView,
  videoSeries: SeriesSummary[] = [series],
  props: {
    embedded?: boolean;
    initialSeriesId?: string;
    initialSeries?: SeriesSummary;
    captureExtraContext?: () => Record<string, unknown> | null;
    onOpenVideo?: (target: { seriesId: string; videoId: string; startSeconds: number }) => void;
    onSeekCurrent?: (positionMs: number) => void;
  } = {},
  displayMode: "release" | "debug" = "release",
) {
  mocks.useWorkspace.mockReturnValue({ mode: "live", displayMode, series: videoSeries, isLoading: false });
  mocks.useAgentConversation.mockReturnValue({
    conversationId: "conversation-1",
    messages: [message],
    progress: {},
    loading: false,
    searching: false,
    cancelling: false,
    error: null,
    send: mocks.send,
    stop: mocks.stop,
  });
  return render(<SearchPage {...props} />);
}

beforeEach(() => {
  mocks.useAuth.mockReturnValue({ canManage: true, activeSource: { kind: "local" } });
  mocks.send.mockReset();
  mocks.send.mockImplementation((_content, _scope, _reference, _options, _context, submission) => submission?.onAccepted?.());
  mocks.stop.mockReset();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:chat-image") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Search assistant presentation", () => {
  it("selects an explicit public source and replaces a different publisher's selection", async () => {
    mocks.useAuth.mockReturnValue({ canManage: false, activeSource: { kind: "public" } });
    const alice = { ...series, seriesId: "alice-1", tenantId: "tenant-alice", name: "Alice one", visibility: "public" as const, access: "public" as const };
    const aliceSecond = { ...alice, seriesId: "alice-2", name: "Alice two" };
    const bob = { ...alice, seriesId: "bob-1", tenantId: "tenant-bob", name: "Bob one" };
    renderPage(assistantMessage("可以提问"), [alice, aliceSecond, bob]);
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("选择检索视频系列"));
    expect(screen.getByRole("checkbox", { name: /Alice one/ })).toBeChecked();
    expect(screen.queryByRole("button", { name: "检索全部视频系列" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /Alice two/ }));
    expect(screen.getByRole("checkbox", { name: /Alice one/ })).toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: /Bob one/ }));
    expect(screen.getByRole("checkbox", { name: /Alice one/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Alice two/ })).not.toBeChecked();
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "查找这段");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(mocks.send).toHaveBeenCalledWith("查找这段", [bob.seriesId], null, {}, null,
      expect.objectContaining({ sourceTenantId: "tenant-bob" }));
  });

  it("uses the loaded watch Series as source even when it is absent from the active library", async () => {
    const shared = { ...series, tenantId: "tenant-alice", visibility: "public" as const, access: "public" as const };
    renderPage(assistantMessage("可以提问"), [], { embedded: true, initialSeriesId: shared.seriesId, initialSeries: shared });
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "刚才发生了什么");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(mocks.send).toHaveBeenCalledWith("刚才发生了什么", [shared.seriesId], null, {}, null,
      expect.objectContaining({ sourceTenantId: "tenant-alice" }));
  });

  it("keeps an empty public source from sending an unbounded search", async () => {
    mocks.useAuth.mockReturnValue({ canManage: false, activeSource: { kind: "public" } });
    renderPage(assistantMessage("可以提问"), []);
    await userEvent.setup().type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "查找这段");
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    expect(screen.getByText("暂无可体验的公开系列")).toHaveAttribute("role", "alert");
    expect(mocks.send).not.toHaveBeenCalled();
  });

  it.each([true, false])("keeps a historical reference on its actual publisher (available=%s)", async (available) => {
    mocks.useAuth.mockReturnValue({ canManage: false, activeSource: { kind: "public" } });
    const alice = { ...series, tenantId: "tenant-alice", name: "Alice series" };
    const bob = { ...series, seriesId: "bob-series", tenantId: "tenant-bob", name: "Bob series" };
    const hit = searchHit("bob-chunk", 1, "Bob clip");
    hit.evidence = { ...hit.evidence, seriesId: bob.seriesId, tenantId: bob.tenantId, seriesName: bob.name };
    renderPage(assistantMessage("历史回答", [hit]), available ? [alice, bob] : [alice]);
    const user = userEvent.setup();
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("Alice series");
    await user.click(screen.getByRole("img", { name: /视频片段预览/ }));
    await user.click(screen.getByRole("button", { name: "引用此片段追问" }));
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "解释这一段");
    if (available) {
      expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("Bob series");
      await user.click(screen.getByRole("button", { name: "发送问题" }));
      expect(mocks.send).toHaveBeenCalledWith("解释这一段", [bob.seriesId],
        { messageId: "assistant-1", videoChunkId: "bob-chunk" }, {}, null,
        expect.objectContaining({ sourceTenantId: "tenant-bob" }));
    } else {
      expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
      expect(screen.getByText("所选视频系列暂不可检索，请重新选择范围。")).toHaveAttribute("role", "alert");
      expect(mocks.send).not.toHaveBeenCalled();
    }
  });

  it("keeps embedded playback questions within the watched publisher", async () => {
    const watched = { ...series, tenantId: "tenant-alice", name: "Watched series" };
    const another = { ...series, seriesId: "other-series", tenantId: "tenant-bob", name: "Other publisher" };
    renderPage(assistantMessage("可以提问"), [another], {
      embedded: true, initialSeriesId: watched.seriesId, initialSeries: watched,
      captureExtraContext: () => ({ playback: { seriesId: watched.seriesId, videoId: "video-1", episodeOrder: 1, positionSeconds: 0 } }),
    });
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("选择检索视频系列"));
    expect(screen.getByRole("checkbox", { name: /Watched series/ })).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: /Other publisher/ })).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "刚才发生了什么");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(mocks.send.mock.calls[0]![1]).toEqual([watched.seriesId]);
    expect(mocks.send.mock.calls[0]![5]).toEqual(expect.objectContaining({ sourceTenantId: "tenant-alice" }));
  });

  it("replaces send with stop while generating and disables stop after acceptance", async () => {
    const view = renderPage({ ...assistantMessage(""), status: "PROCESSING", phase: "EVALUATING" });
    const running = { ...mocks.useAgentConversation.mock.results.at(-1)!.value, searching: true };
    mocks.useAgentConversation.mockReturnValue(running);
    view.rerender(<SearchPage />);
    expect(screen.queryByRole("button", { name: "发送问题" })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "停止生成" }));
    expect(mocks.stop).toHaveBeenCalledOnce();
    expect(mocks.send).not.toHaveBeenCalled();
    mocks.useAgentConversation.mockReturnValue({ ...running, cancelling: true });
    view.rerender(<SearchPage />);
    expect(screen.getByRole("button", { name: "正在停止" })).toBeDisabled();
  });

  it.each(["overrides", "selection"] as const)("ignores stored topK in %s and preserves other debug options", async (storageFormat) => {
    const key = api.identityStorageKey("seekino-agent-message-options");
    const overrides = { maxLoopTurns: 9, maxModelCallsPerTurn: 4, maxToolCallsPerTurn: 8 };
    const serverOptions = {
      topK: { defaultValue: 10, minimum: 1, maximum: 20 },
      maxLoopTurns: { defaultValue: 100, minimum: 1, maximum: 100 },
      maxModelCallsPerTurn: { defaultValue: 6, minimum: 1, maximum: 12 },
      maxToolCallsPerTurn: { defaultValue: 24, minimum: 1, maximum: 48 },
    };
    localStorage.setItem(key, JSON.stringify(storageFormat === "overrides"
      ? { overrides: { topK: 3, ...overrides } }
      : { serverOptions, selection: { topK: 3, ...overrides } }));
    vi.spyOn(api, "getAgentMessageOptions").mockResolvedValue(serverOptions);
    try {
      const view = renderPage(assistantMessage("可以提问"), [series], {}, "debug");
      expect(await screen.findByText("最多 9 轮")).toBeInTheDocument();
      expect(screen.getByLabelText("配置本次回答")).toBeInTheDocument();
      expect(screen.queryByLabelText("单次检索候选片段数量")).not.toBeInTheDocument();
      const user = userEvent.setup();
      await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "你好");
      await user.click(screen.getByRole("button", { name: "发送问题" }));
      expect(mocks.send).toHaveBeenLastCalledWith("你好", [], null, overrides, null, expect.objectContaining({ attachments: [], onAccepted: expect.any(Function) }));
      mocks.useWorkspace.mockReturnValue({ mode: "live", displayMode: "release", series: [series], isLoading: false });
      view.rerender(<SearchPage />);
      expect(screen.queryByLabelText("配置本次回答")).not.toBeInTheDocument();
      await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "继续");
      await user.click(screen.getByRole("button", { name: "发送问题" }));
      expect(mocks.send).toHaveBeenLastCalledWith("继续", [], null, {}, null, expect.objectContaining({ attachments: [], onAccepted: expect.any(Function) }));
      expect(JSON.parse(localStorage.getItem(key)!)).toEqual({ overrides });
    } finally {
      localStorage.removeItem(key);
    }
  });

  it("uses the current ready series for an embedded conversation and opens evidence in the whole-video player", async () => {
    const hit = searchHit("chunk-1", 1, "片段首帧");
    const onOpenVideo = vi.fn();
    renderPage(assistantMessage("找到片段", [hit]), [series], {
      embedded: true, initialSeriesId: series.seriesId, onOpenVideo,
    });
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "当前对话", level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Seekino 助手" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("打开对话记录")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建对话" })).toBeInTheDocument();
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent(series.name);
    expect(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }).closest(".page")).toBeNull();
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "这段发生了什么？");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(mocks.send).toHaveBeenCalledWith("这段发生了什么？", [series.seriesId], null, {}, null, expect.objectContaining({ attachments: [], onAccepted: expect.any(Function) }));

    await user.click(screen.getByRole("img", { name: /00:12–00:15 视频片段预览/ }));
    expect(onOpenVideo).toHaveBeenCalledWith({ tenantId: series.tenantId, seriesId: series.seriesId, videoId: "video-1", startSeconds: hit.evidence.startSeconds });
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("片段详情")).not.toBeInTheDocument();
  });

  it("leaves interaction handling to its parent and captures the latest context on submit", async () => {
    const initialContext = {
      playback: { seriesId: series.seriesId, videoId: "video-1", episodeOrder: 1, positionSeconds: 12_000 },
    };
    const extraContext = {
      playback: { seriesId: series.seriesId, videoId: "video-1", episodeOrder: 1, positionSeconds: 14_000 },
    };
    let currentContext = initialContext;
    const captureExtraContext = vi.fn(() => currentContext);
    const view = renderPage(assistantMessage("可以提问"), [series], {
      embedded: true,
      initialSeriesId: series.seriesId,
      captureExtraContext,
    });
    const chatRef = createRef<SearchPageHandle>();
    view.rerender(
      <SearchPage
        ref={chatRef}
        embedded
        initialSeriesId={series.seriesId}
        captureExtraContext={captureExtraContext}
      />,
    );
    const textbox = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });

    chatRef.current?.focusComposer();
    expect(textbox).toHaveFocus();
    expect(captureExtraContext).not.toHaveBeenCalled();

    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "这里继续讲了什么？", { skipClick: true });
    currentContext = extraContext;
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(captureExtraContext).toHaveBeenCalledOnce();
    expect(mocks.send).toHaveBeenCalledWith(
      "这里继续讲了什么？",
      [series.seriesId],
      null,
      {},
      extraContext,
      expect.objectContaining({ attachments: [], onAccepted: expect.any(Function) }),
    );
  });

  it("allows retrying the same question when playback context becomes available", async () => {
    const extraContext = {
      playback: { seriesId: series.seriesId, videoId: "video-1", episodeOrder: 1, positionSeconds: 0 },
    };
    const captureExtraContext = vi.fn()
      .mockReturnValueOnce(null)
      .mockReturnValueOnce(extraContext);
    renderPage(assistantMessage("可以提问"), [series], {
      embedded: true,
      initialSeriesId: series.seriesId,
      captureExtraContext,
    });
    const textbox = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });
    textbox.focus();
    const user = userEvent.setup();
    await user.type(textbox, "开头讲了什么？", { skipClick: true });
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(mocks.send).not.toHaveBeenCalled();
    expect(textbox).toHaveValue("开头讲了什么？");

    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(captureExtraContext).toHaveBeenCalledTimes(2);
    expect(mocks.send).toHaveBeenCalledWith("开头讲了什么？", [series.seriesId], null, {}, extraContext, expect.objectContaining({ attachments: [], onAccepted: expect.any(Function) }));
  });

  it("keeps the question and does not send when playback context is unavailable on submit", async () => {
    const captureExtraContext = vi.fn(() => null);
    renderPage(assistantMessage("可以提问"), [series], {
      embedded: true,
      initialSeriesId: series.seriesId,
      captureExtraContext,
    });
    const user = userEvent.setup();
    const textbox = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });
    textbox.focus();
    await user.type(textbox, "这里讲了什么？", { skipClick: true });
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(captureExtraContext).toHaveBeenCalledOnce();
    expect(mocks.send).not.toHaveBeenCalled();
    expect(textbox).toHaveValue("这里讲了什么？");
    expect(screen.getByText("当前播放位置尚未就绪，请稍后重试")).toHaveAttribute("role", "alert");
  });

  it("sends an example question with one click from the empty conversation", async () => {
    renderPage(assistantMessage("占位"));
    cleanup();
    mocks.useAgentConversation.mockReturnValue({
      conversationId: "conversation-1", messages: [], progress: {}, loading: false, searching: false, error: null, send: mocks.send,
    });
    render(<SearchPage />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "乐队第一次站上舞台演出" }));
    expect(mocks.send).toHaveBeenCalledWith("乐队第一次站上舞台演出", [], null, {}, null, expect.objectContaining({ attachments: [], onAccepted: expect.any(Function) }));
  });

  it("turns answer timestamps into seek buttons for the episode being watched", async () => {
    const onSeekCurrent = vi.fn();
    renderPage(assistantMessage("当前停在 **10分38秒** 左右，片尾在 01:02:03；画面比例 16:9。"), [series], {
      embedded: true, initialSeriesId: series.seriesId, onSeekCurrent,
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "10:38" }));
    expect(onSeekCurrent).toHaveBeenLastCalledWith(638_000);
    await user.click(screen.getByRole("button", { name: "62:03" }));
    expect(onSeekCurrent).toHaveBeenLastCalledWith(3_723_000);
    expect(screen.queryByRole("button", { name: "16:9" })).not.toBeInTheDocument();
  });

  it("keeps answer timestamps as plain text outside the watch page", () => {
    renderPage(assistantMessage("大约在 10:38 出现。"));
    expect(screen.queryByRole("button", { name: "10:38" })).not.toBeInTheDocument();
    expect(screen.getByText("大约在 10:38 出现。")).toBeInTheDocument();
  });

  it("formats displayed fractional timestamps while preserving exact click targets and links", async () => {
    const onSeekCurrent = vi.fn();
    renderPage(assistantMessage("## 09:23.400 的重逢\n\n见 **09:57.500**，也可看 [10:02.700](https://example.com/?atSeconds=602700)。"), [series], { onSeekCurrent });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "09:23" }));
    expect(onSeekCurrent).toHaveBeenLastCalledWith(563_400);
    await user.click(screen.getByRole("button", { name: "09:57" }));
    expect(onSeekCurrent).toHaveBeenLastCalledWith(597_500);
    expect(screen.getByRole("link", { name: "10:02" })).toHaveAttribute("href", "https://example.com/?atSeconds=602700");
    expect(screen.queryByText(/\.400|\.500|\.700/)).not.toBeInTheDocument();
  });

  it("formats times in standalone answers without changing stored Markdown", () => {
    const content = "片段 [540000,546800)，重逢在 09:23.400；第13集，画面16:9。";
    const message = assistantMessage(content);
    renderPage(message);
    expect(screen.getByText("片段 09:00–09:06，重逢在 09:23；第13集，画面16:9。")).toBeInTheDocument();
    expect(message.content).toBe(content);
  });

  it("keeps PPE cards concise and shows raw evidence only in the opened detail panel", async () => {
    const hit = searchHit("chunk-1", 1, "首帧");
    hit.evidence.videoCompact = "";
    hit.evidence.windowSummary = "[540000,546800) 原始 Timeline 人物标记";
    hit.selectionReason.summary = "09:23.400–09:57.500：两人重逢。";
    const message = assistantMessage("重逢在09:23.400。", [hit]);
    message.searchResult!.partial = true;
    message.searchResult!.warnings = ["片段 chunk-1：缺少带原视频时间位置的已登记关键帧"];
    const view = renderPage(message);
    expect(screen.getByText("09:23–09:57：两人重逢。").tagName).toBe("STRONG");
    expect(screen.getByText("本次回答仅覆盖当前可检索的视频。")).toBeInTheDocument();
    expect(screen.queryByText(/540000|Timeline|角色甲：|已登记关键帧/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "原始证据" })).not.toBeInTheDocument();
    mocks.useWorkspace.mockReturnValue({ mode: "live", displayMode: "debug", series: [series], isLoading: false });
    view.rerender(<SearchPage />);
    expect(screen.queryByText(hit.evidence.windowSummary)).not.toBeInTheDocument();
    expect(screen.queryByText(/角色甲：/)).not.toBeInTheDocument();
    expect(screen.getByText(message.searchResult!.warnings[0]!)).toBeInTheDocument();
    expect(screen.getByText("重逢在09:23.400。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "原始证据" })).not.toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("img", { name: /00:12–00:15 视频片段预览/ }));
    expect(screen.getByRole("button", { name: "原始证据" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "前后语境" }));
    expect(screen.getByText(hit.evidence.windowSummary)).toBeInTheDocument();
    const card = screen.getByLabelText("检索助手提交的视频片段");
    expect(within(card).queryByText(hit.evidence.windowSummary)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "原始证据" }));
    expect(screen.getByText(/角色甲：/)).toBeInTheDocument();
    expect(within(card).queryByText(/角色甲：/)).not.toBeInTheDocument();
    mocks.useWorkspace.mockReturnValue({ mode: "live", displayMode: "release", series: [series], isLoading: false });
    view.rerender(<SearchPage />);
    expect(screen.queryByText(/540000|Timeline|角色甲：|已登记关键帧/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "原始证据" })).not.toBeInTheDocument();
  });

  it("states when the watched series is not searchable instead of presenting a global scope as current", async () => {
    renderPage(assistantMessage("可以继续聊天"), [{ ...series, status: "PARTIAL" }], {
      embedded: true, initialSeriesId: series.seriesId,
    });
    const user = userEvent.setup();

    expect(screen.getByText("检索范围：全部系列")).toBeVisible();
    expect(screen.getByText("本系列未完成解析")).toBeVisible();
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("全部视频系列");
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "你好");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(mocks.send).toHaveBeenCalledWith("你好", [], null, {}, null, expect.objectContaining({ attachments: [], onAccepted: expect.any(Function) }));
  });

  it("allows a conversation without ready videos and displays a web source link", async () => {
    renderPage({
      ...assistantMessage("[公开资料](https://example.com/source)"),
      seriesIds: [],
    }, []);
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "Seekino 助手", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "公开资料" })).toHaveAttribute("href", "https://example.com/source");
    expect(screen.queryByText(/可用视频范围/)).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "回答状态" })).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "你好");
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(mocks.send).toHaveBeenCalledWith("你好", [], null, {}, null, expect.objectContaining({ attachments: [], onAccepted: expect.any(Function) }));
    expect(screen.getByText("问题已发送，正在处理")).toBeInTheDocument();
  });

  const traceCases: Array<{ label: string; weights: RetrievalChannelWeights }> = [
    { label: "historical ASR", weights: { transcript: 0.2, semantic: 0.5, visual: 0.3 } },
    { label: "lexical", weights: { lexical: 0.2, semantic: 0.5, visual: 0.3 } },
  ];

  it.each(traceCases)("waits for answer completion before reporting candidates without video with $label trace", ({ weights }) => {
    const message: AgentMessageView = {
      ...assistantMessage("只生成了文字"),
      status: "PROCESSING",
      phase: "RETRIEVING",
      searchResult: {
        seriesIds: [series.seriesId],
        searchedVideoIds: ["video-1"],
        partial: false,
        warnings: [],
        hits: [],
        retrievalTrace: [{
          query: "雨天门口",
          topK: 8,
          strategyVersion: "test",
          requestedWeights: weights,
          effectiveWeights: weights,
          resultCount: 2,
          videoChunkIds: ["chunk-1", "chunk-2"],
        }],
      },
      renderBlocks: [
        { type: "markdown", markdown: "只生成了文字" },
        { type: "tool", callId: "video-lookup", tool: "search_video_graph", status: "completed" },
      ],
    };

    const view = renderPage(message);

    expect(screen.getByRole("status", { name: "查找视频资料" })).toHaveTextContent("已完成");
    expect(screen.queryByText("检索到了候选片段，但本次回答未附带可播放视频，请重试。")).not.toBeInTheDocument();
    expect(screen.queryByText("没有检索到可展示的视频片段，试试换个描述。")).not.toBeInTheDocument();

    mocks.useAgentConversation.mockReturnValue({
      ...mocks.useAgentConversation.mock.results.at(-1)!.value,
      messages: [{ ...message, status: "COMPLETED", phase: null }],
    });
    view.rerender(<SearchPage />);

    expect(screen.getByText("检索到了候选片段，但本次回答未附带可播放视频，请重试。")).toBeInTheDocument();
    expect(screen.queryByText("没有检索到可展示的视频片段，试试换个描述。")).not.toBeInTheDocument();
  });

  it("renders GFM while dropping raw HTML, unsafe links and remotely loaded model images", () => {
    const content = [
      "## 核心事件",
      "",
      "- 雨天相遇",
      "- **红绿灯**切换",
      "",
      "| 片段 | 结论 |",
      "| --- | --- |",
      "| EP 01 | 可核验 |",
      "",
      "<script>window.bad = true</script>",
      "",
      "![远程图片](https://example.com/tracker.png)",
      "",
      "[安全资料](https://example.com/source)",
      "",
      "[危险链接](javascript:evil)",
    ].join("\n");
    const view = renderPage(assistantMessage(content));

    expect(screen.getByRole("heading", { name: "核心事件", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "可核验" })).toBeInTheDocument();
    expect(screen.getByText("红绿灯").tagName).toBe("STRONG");
    expect(view.container.querySelector("script")).toBeNull();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText("[图片：远程图片]")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "安全资料" })).toHaveAttribute("href", "https://example.com/source");
    expect(screen.getByRole("link", { name: "安全资料" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "安全资料" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText("危险链接").closest("a")).toBeNull();
  });

  it("renders a live tool between Markdown and video parts in the message body", () => {
    const hit = searchHit("chunk-1", 1, "演出画面");
    renderPage({
      ...assistantMessage("兼容正文", [hit]), status: "PROCESSING", phase: "RETRIEVING",
      renderBlocks: [
        { type: "markdown", markdown: "我先帮你查找演出场面。" },
        { type: "tool", callId: "video-lookup", tool: "search_video_graph", status: "inProgress", query: "特别燃的演出" },
        { type: "markdown", markdown: "接下来核对这段画面。" },
        { type: "video", videoChunkId: "chunk-1", startSeconds: 12_000, endSeconds: 15_000 },
      ],
    });
    const before = screen.getByText("我先帮你查找演出场面。");
    const tool = screen.getByRole("status", { name: "查找视频资料" });
    const after = screen.getByText("接下来核对这段画面。");
    const video = screen.getByRole("img", { name: /00:12–00:15 视频片段预览/ });
    expect(before.compareDocumentPosition(tool) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(tool.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(after.compareDocumentPosition(video) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(tool).not.toHaveTextContent("特别燃的演出");
    expect(tool.querySelector("p")).toBeNull();
    expect(tool).toHaveAttribute("data-active", "true");
    expect(tool.closest("details")).toBeNull();
    expect(screen.queryByText("兼容正文")).not.toBeInTheDocument();
  });

  it.each(["completed", "failed"] as const)("updates a web tool to %s in place without video evidence", (status) => {
    const tool: AgentRenderBlock = { type: "tool", callId: "web-lookup", tool: "search_web", status: "inProgress", query: "<img src=x onerror=alert(1)>" };
    const before: AgentRenderBlock = { type: "markdown", markdown: "我先搜索网页。" };
    const after: AgentRenderBlock = { type: "markdown", markdown: "搜索后的说明。" };
    const message: AgentMessageView = { ...assistantMessage(""), status: "PROCESSING", renderBlocks: [before, tool] };
    const view = renderPage(message);
    const row = screen.getByRole("status", { name: "搜索网页资料" });
    expect(row).toHaveAttribute("data-active", "true");
    expect(row).not.toHaveTextContent(tool.query!);
    expect(row.querySelector("p")).toBeNull();
    expect(row.querySelector("img")).toBeNull();
    mocks.useAgentConversation.mockReturnValue({
      ...mocks.useAgentConversation.mock.results.at(-1)!.value,
      messages: [{ ...message, renderBlocks: [before, { ...tool, status }, after] }],
    });
    view.rerender(<SearchPage />);
    expect(screen.getByRole("status", { name: "搜索网页资料" })).toBe(row);
    expect(row).toHaveAttribute("data-active", "false");
    expect(row).toHaveTextContent(status === "completed" ? "已完成" : "调用失败");
    expect(row).not.toHaveTextContent(tool.query!);
    expect(row.compareDocumentPosition(screen.getByText("搜索后的说明。")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows raw tool queries only in debug and removes them immediately when switching to release", () => {
    const tools = [
      { tool: "search_video_graph", status: "inProgress", query: "特别燃的演出", label: "查找视频资料", queryLabel: "检索目标", statusLabel: "进行中" },
      { tool: "search_web", status: "completed", query: "<img src=x onerror=alert(1)>", label: "搜索网页资料", queryLabel: "搜索词", statusLabel: "已完成" },
      { tool: "understand_current_video", status: "failed", query: "舞台上的人物", label: "查看当前视频", queryLabel: "核验目标", statusLabel: "调用失败" },
    ] as const;
    const view = renderPage({
      ...assistantMessage(""), status: "PROCESSING",
      renderBlocks: tools.map(({ tool, status, query }, index) => ({ type: "tool", callId: `tool-${index}`, tool, status, query })),
    }, [series], {}, "debug");
    const rows = tools.map(({ label, queryLabel, query, statusLabel, status }) => {
      const row = screen.getByRole("status", { name: label });
      expect(within(row).getByText(`${queryLabel}：${query}`).tagName).toBe("P");
      expect(row).toHaveTextContent(statusLabel);
      expect(row).toHaveAttribute("data-status", status);
      expect(row.querySelector("img")).toBeNull();
      return row;
    });

    mocks.useWorkspace.mockReturnValue({ mode: "live", displayMode: "release", series: [series], isLoading: false });
    view.rerender(<SearchPage />);

    tools.forEach(({ label, query, statusLabel, status }, index) => {
      const row = screen.getByRole("status", { name: label });
      expect(row).toBe(rows[index]);
      expect(row).toHaveTextContent(label);
      expect(row).toHaveTextContent(statusLabel);
      expect(row).toHaveAttribute("data-status", status);
      expect(row).not.toHaveTextContent(query);
      expect(row.querySelector("p")).toBeNull();
    });
  });

  it.each(["COMPLETED", "FAILED"] as const)("stops an unconfirmed tool's shimmer when the message is %s", (status) => {
    renderPage({
      ...assistantMessage("已保留的说明"), status,
      renderBlocks: [
        { type: "markdown", markdown: "已保留的说明" },
        { type: "tool", callId: "current-video", tool: "understand_current_video", status: "inProgress" },
      ],
    });
    const tool = screen.getByRole("status", { name: "查看当前视频" });
    expect(tool).toHaveAttribute("data-status", "inProgress");
    expect(tool).toHaveAttribute("data-active", "false");
    expect(tool).toHaveTextContent("未确认完成");
    expect(tool).not.toHaveTextContent("已完成");
  });

  it("keeps tool-shaped JSON in Markdown as ordinary message text", () => {
    const json = '{"type":"tool","callId":"fake","tool":"search_web","status":"inProgress"}';
    renderPage({ ...assistantMessage(json), status: "PROCESSING" });
    expect(screen.getByText(json)).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "搜索网页资料" })).not.toBeInTheDocument();
  });

  it("renders persisted Markdown and trusted video references in agent-selected order", () => {
    const hits = [searchHit("chunk-1", 1, "第一段首帧"), searchHit("chunk-2", 2, "第二段首帧")];
    const message = {
      ...assistantMessage("兼容正文", hits),
      renderBlocks: [
        { type: "markdown" as const, markdown: "## 开场说明" },
        { type: "video" as const, videoChunkId: "chunk-2", startSeconds: 22_000, endSeconds: 25_000 },
        { type: "markdown" as const, markdown: "两段之间的解释" },
        { type: "video" as const, videoChunkId: "unknown-chunk", startSeconds: 1_000, endSeconds: 2_000 },
        { type: "video" as const, videoChunkId: "chunk-1", startSeconds: 12_000, endSeconds: 15_000 },
      ],
    };

    renderPage(message);

    const introduction = screen.getByRole("heading", { name: "开场说明" });
    const secondPreview = screen.getByRole("img", { name: /00:22–00:25 视频片段预览/ });
    const bridge = screen.getByText("两段之间的解释");
    const firstPreview = screen.getByRole("img", { name: /00:12–00:15 视频片段预览/ });
    expect(introduction.compareDocumentPosition(secondPreview) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(secondPreview.compareDocumentPosition(bridge) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(bridge.compareDocumentPosition(firstPreview) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByRole("img")).toHaveLength(2);
    expect(screen.queryByText("兼容正文")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();
    expect(screen.getByText("00:22–00:25")).toBeInTheDocument();
  });

  it("uses the agent-selected time range instead of the source chunk range", async () => {
    const hit = searchHit("chunk-1", 1, "精确片段首帧");
    const message = {
      ...assistantMessage("精确片段", [hit]),
      renderBlocks: [
        { type: "video" as const, videoChunkId: hit.evidence.videoChunkId, startSeconds: 12_000, endSeconds: 13_000 },
      ],
    };
    renderPage(message);
    const user = userEvent.setup();

    expect(screen.getByText("00:12–00:13")).toBeInTheDocument();
    expect(screen.queryByText("00:12–00:15")).not.toBeInTheDocument();
    await user.click(screen.getByRole("img", { name: /00:12–00:13 视频片段预览/ }));
    const video = screen.getByLabelText<HTMLVideoElement>("视频证据");
    Object.defineProperty(video, "readyState", { value: HTMLMediaElement.HAVE_METADATA, configurable: true });
    video.dispatchEvent(new Event("loadedmetadata"));

    expect(video.currentTime).toBe(12);
    expect(screen.getByRole("slider", { name: "视频片段播放进度" })).toHaveAttribute("max", "1000");
    expect(screen.getByText("00:00 / 00:01")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "引用此片段追问" }));
    expect(screen.getByText(/已引用.*00:12–00:13/)).toBeInTheDocument();
  });

  it("updates an opened player's range when a streamed video block is revised", async () => {
    const hit = searchHit("chunk-1", 1, "首帧");
    const message = {
      ...assistantMessage("正在说明片段", [hit]), status: "PROCESSING" as const,
      renderBlocks: [{ type: "video" as const, videoChunkId: "chunk-1", startSeconds: 12_000, endSeconds: 13_000 }],
    };
    const view = renderPage(message);
    await userEvent.setup().click(screen.getByRole("img", { name: /00:12–00:13 视频片段预览/ }));
    expect(screen.getByRole("slider", { name: "视频片段播放进度" })).toHaveAttribute("max", "1000");

    mocks.useAgentConversation.mockReturnValue({
      ...mocks.useAgentConversation.mock.results.at(-1)!.value,
      messages: [{ ...message, version: 2, renderBlocks: [{ type: "video", videoChunkId: "chunk-1", startSeconds: 13_250, endSeconds: 14_500 }] }],
    });
    view.rerender(<SearchPage />);
    const video = screen.getByLabelText<HTMLVideoElement>("视频证据");
    Object.defineProperty(video, "readyState", { value: HTMLMediaElement.HAVE_METADATA, configurable: true });
    fireEvent.loadedMetadata(video);

    expect(video.currentTime).toBe(13.25);
    expect(screen.getByRole("slider", { name: "视频片段播放进度" })).toHaveAttribute("max", "1250");
    expect(within(screen.getByLabelText("片段详情")).getByText("片段范围 00:13–00:14")).toBeInTheDocument();
  });

  it("closes a withdrawn video's detail even when its source hit supports another merged block", async () => {
    const hits = [searchHit("chunk-1", 1, "第一段首帧"), searchHit("chunk-2", 2, "第二段首帧")];
    Object.assign(hits[1]!.evidence, { startSeconds: 15_000, endSeconds: 18_000 });
    const message = { ...assistantMessage("正在说明片段", hits), status: "PROCESSING" as const };
    const view = renderPage(message);
    await userEvent.setup().click(screen.getByRole("img", { name: /00:12–00:15 视频片段预览/ }));
    expect(screen.getByLabelText("片段详情")).toBeInTheDocument();

    mocks.useAgentConversation.mockReturnValue({
      ...mocks.useAgentConversation.mock.results.at(-1)!.value,
      messages: [{ ...message, version: 2, renderBlocks: [{ type: "video", videoChunkId: "chunk-2", startSeconds: 12_000, endSeconds: 18_000 }] }],
    });
    view.rerender(<SearchPage />);

    expect(screen.queryByLabelText("片段详情")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: /00:12–00:18 视频片段预览/ })).toBeInTheDocument();
    expect(screen.getByText("1 段")).toBeInTheDocument();
  });

  it("renders two source chunks as one final clip with the full playback and reference range", async () => {
    const left = searchHit("chunk-1", 7, "连续片段首帧");
    const right = searchHit("chunk-2", 8, "相邻来源首帧");
    Object.assign(left.evidence, { startSeconds: 540_000, endSeconds: 600_000, durationSeconds: 1_440_000 });
    Object.assign(right.evidence, { startSeconds: 600_000, endSeconds: 660_000, durationSeconds: 1_440_000 });
    const message = {
      ...assistantMessage("连续对话", [left, right]),
      renderBlocks: [
        { type: "video" as const, videoChunkId: "chunk-1", startSeconds: 564_500, endSeconds: 656_100 },
      ],
    };
    renderPage(message);
    const user = userEvent.setup();

    expect(screen.getAllByLabelText("检索助手提交的视频片段")).toHaveLength(1);
    expect(screen.getByText("1 段")).toBeInTheDocument();
    expect(within(screen.getByLabelText("检索助手提交的视频片段")).getByText("1")).toBeInTheDocument();
    expect(screen.getByText("09:24–10:56")).toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(1);
    await user.click(screen.getByRole("img", { name: /09:24–10:56 视频片段预览/ }));
    const video = screen.getByLabelText<HTMLVideoElement>("视频证据");
    Object.defineProperty(video, "readyState", { value: HTMLMediaElement.HAVE_METADATA, configurable: true });
    fireEvent.loadedMetadata(video);

    expect(video.currentTime).toBe(564.5);
    expect(screen.getByRole("slider", { name: "视频片段播放进度" })).toHaveAttribute("max", "91600");
    video.currentTime = 660;
    fireEvent.timeUpdate(video);
    expect(video.currentTime).toBe(656.1);
    await user.click(screen.getByRole("button", { name: "引用此片段追问" }));
    expect(screen.getByText(/已引用.*09:24–10:56/)).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "继续解释这段");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(mocks.send).toHaveBeenCalledWith("继续解释这段", [], { messageId: message.messageId, videoChunkId: "chunk-1" }, {}, null, expect.objectContaining({ attachments: [], onAccepted: expect.any(Function) }));
  });

  it.each([
    { label: "a negative start", startSeconds: -1, endSeconds: 13_000 },
    { label: "an end beyond the source video", startSeconds: 12_000, endSeconds: 60_001 },
    { label: "an empty range", startSeconds: 12_000, endSeconds: 12_000 },
    { label: "a reversed range", startSeconds: 13_000, endSeconds: 12_000 },
    { label: "a fractional start", startSeconds: 12_000.5, endSeconds: 13_000 },
    { label: "a fractional end", startSeconds: 12_000, endSeconds: 13_000.5 },
    { label: "a non-finite end", startSeconds: 12_000, endSeconds: Infinity },
  ])("drops a final clip with $label", async ({ startSeconds, endSeconds }) => {
    const hit = searchHit("chunk-1", 1, "越界片段首帧");
    const message = {
      ...assistantMessage("越界片段", [hit]),
      renderBlocks: [
        { type: "markdown" as const, markdown: "越界片段" },
        { type: "video" as const, videoChunkId: hit.evidence.videoChunkId, startSeconds, endSeconds },
      ],
    };
    renderPage(message);

    expect(await screen.findByText("越界片段")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /视频片段预览/ })).not.toBeInTheDocument();
    expect(screen.queryByText("00:12–00:15")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();
  });

  it("does not synthesize canonical video blocks for a legacy message without renderBlocks", () => {
    const hit = searchHit("chunk-1", 1, "旧消息首帧");
    const { renderBlocks: _renderBlocks, ...legacyMessage } = assistantMessage(
      "旧消息只有正文",
      [hit],
    );

    renderPage(legacyMessage as AgentMessageView);

    expect(screen.getByText("旧消息只有正文")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /视频片段预览/ })).not.toBeInTheDocument();
    expect(screen.queryByText("00:12–00:15")).not.toBeInTheDocument();
  });

  it("shows paused previews until selection and keeps exactly one selected player mounted", async () => {
    const hits = [searchHit("chunk-1", 1, "第一段首帧"), searchHit("chunk-2", 2, "第二段首帧")];
    renderPage(assistantMessage("找到两个候选片段。", hits));
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "视频片段" })).toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(2);
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: /00:12–00:15 视频片段预览/ }).querySelector("video")).not.toHaveAttribute("autoplay");

    await user.click(screen.getByRole("img", { name: /00:12–00:15 视频片段预览/ }));
    const firstVideo = screen.getByLabelText<HTMLVideoElement>("视频证据");
    expect(screen.getAllByLabelText("视频证据")).toHaveLength(1);
    expect(firstVideo).toHaveAttribute("preload", "metadata");

    await user.click(screen.getByRole("img", { name: /00:22–00:25 视频片段预览/ }));
    const secondVideo = screen.getByLabelText<HTMLVideoElement>("视频证据");
    expect(screen.getAllByLabelText("视频证据")).toHaveLength(1);
    expect(secondVideo).not.toBe(firstVideo);

    await user.click(screen.getByRole("button", { name: "关闭片段详情" }));
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(2);
  });

  it.each([true, false])("previews the final selected start time with a registered thumbnail: %s", async (hasThumbnail) => {
    const hit = searchHit("chunk-1", 1, "不会显示的首帧");
    if (!hasThumbnail) hit.evidence.thumbnail = null;
    const message = assistantMessage("找到一个可播放片段。", [hit]);
    message.renderBlocks = [{ type: "video", videoChunkId: hit.evidence.videoChunkId, startSeconds: 13_250, endSeconds: 14_500 }];
    renderPage(message);
    const user = userEvent.setup();

    const preview = screen.getByRole("img", { name: /00:13–00:14 视频片段预览/ });
    const frame = preview.querySelector("video")!;
    expect(frame).toHaveAttribute("src", hit.evidence.playbackUrl);
    expect(frame).not.toHaveAttribute("poster");
    expect(screen.queryByRole("img", { name: "不会显示的首帧" })).not.toBeInTheDocument();
    expect(frame).not.toHaveAttribute("autoplay");
    expect(frame.muted).toBe(true);
    Object.defineProperty(frame, "duration", { value: 60, configurable: true });
    fireEvent.loadedMetadata(frame);
    expect(frame.currentTime).toBe(13.25);
    Object.defineProperty(frame, "readyState", { value: 2, configurable: true });
    fireEvent.seeked(frame);
    expect(frame).toBeVisible();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(screen.queryByText(/暂无首帧/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();

    await user.click(preview);
    expect(screen.getByLabelText("视频证据")).toHaveAttribute("preload", "metadata");
  });

  it("keeps a preview card clickable when loading its video frame fails", async () => {
    const hit = searchHit("chunk-without-thumbnail", 1, "不会显示的首帧");
    hit.evidence.thumbnail = null;
    renderPage(assistantMessage("找到一个可播放片段。", [hit]));
    const preview = screen.getByRole("img", { name: /视频片段预览/ });
    fireEvent.error(preview.querySelector("video")!);

    expect(preview.querySelector("video")).toBeNull();
    await userEvent.setup().click(preview);
    expect(screen.getByLabelText("视频证据")).toBeInTheDocument();
  });

  it("does not request a preview for unavailable media", () => {
    const hit = searchHit("chunk-without-media", 1, "不会显示的首帧");
    hit.evidence.thumbnail = null;
    hit.evidence.playbackStatus = "MISSING";
    hit.evidence.playbackUrl = null;
    const view = renderPage(assistantMessage("视频暂不可用。", [hit]));

    expect(view.container.querySelector("video")).toBeNull();
    expect(screen.getByText("视频暂不可播放")).toBeInTheDocument();
  });

  it("uses a registered thumbnail when playback media is unavailable", () => {
    const hit = searchHit("chunk-without-media", 1, "已登记首帧");
    hit.evidence.playbackStatus = "MISSING";
    hit.evidence.playbackUrl = null;
    const view = renderPage(assistantMessage("视频暂不可用。", [hit]));

    expect(view.container.querySelector("video")).toBeNull();
    expect(screen.getByRole("img", { name: "已登记首帧" })).toHaveAttribute("src", hit.evidence.thumbnail!.url);
    expect(screen.getByRole("img", { name: "已登记首帧" })).toHaveAttribute("loading", "lazy");
  });
});

describe("Chat image attachments", () => {
  beforeEach(() => {
    vi.spyOn(api, "listAgentConversations").mockResolvedValue({ items: [], hasMore: false, nextCursor: null, total: 0 });
  });
  const uploaded = { mediaUri: "media://image-1", mediaType: "image/png", sizeBytes: 12, previewUrl: "/api/agent/attachments/content?mediaUri=image-1" };

  it("places the plus and send buttons inside the composer and uploads selected files before sending references", async () => {
    const upload = vi.spyOn(api, "uploadAgentAttachment").mockResolvedValue(uploaded);
    renderPage(assistantMessage("可以提问"));
    const user = userEvent.setup();
    const textbox = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });
    expect(screen.getByRole("button", { name: "添加图片" }).closest("form")).toBe(textbox.closest("form"));
    expect(screen.getByRole("button", { name: "发送问题" }).closest("form")).toBe(textbox.closest("form"));
    const file = new File(["image bytes"], "表情.png", { type: "image/png" });
    await user.upload(screen.getByLabelText("选择聊天图片"), file);
    expect(upload).toHaveBeenCalledWith("conversation-1", file, expect.any(AbortSignal));
    expect(screen.getByText("已上传")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    await user.type(textbox, "这张图出自哪里？");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(mocks.send).toHaveBeenCalledWith("这张图出自哪里？", [], null, {}, null, {
      attachments: [{ mediaUri: uploaded.mediaUri }], onAccepted: expect.any(Function),
    });
    expect(textbox).toHaveValue("");
    expect(screen.queryByLabelText("待发送图片")).not.toBeInTheDocument();
  });

  it("retains text and uploaded images when the service rejects attachment input", async () => {
    vi.spyOn(api, "uploadAgentAttachment").mockResolvedValue(uploaded);
    mocks.send.mockImplementation(() => undefined);
    const view = renderPage(assistantMessage("可以提问"));
    const user = userEvent.setup();
    const textbox = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });
    await user.upload(screen.getByLabelText("选择聊天图片"), new File(["bytes"], "图.png", { type: "image/png" }));
    await user.type(textbox, "找到这张图");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    const previous = mocks.useAgentConversation.mock.results.at(-1)!.value;
    mocks.useAgentConversation.mockReturnValue({ ...previous, error: "附件已上传，当前回答服务尚未接入附件输入。" });
    view.rerender(<SearchPage />);
    expect(screen.getByRole("alert")).toHaveTextContent("当前回答服务尚未接入附件输入");
    expect(textbox).toHaveValue("找到这张图");
    expect(screen.getByRole("img", { name: "图.png" })).toBeInTheDocument();
    expect(mocks.send).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "移除 图.png" }));
    expect(textbox).toHaveFocus();
    expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled();
  });

  it("blocks sending while uploading or failed and retries the same file", async () => {
    let rejectUpload!: (error: Error) => void;
    const upload = vi.spyOn(api, "uploadAgentAttachment")
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectUpload = reject; }))
      .mockResolvedValue(uploaded);
    renderPage(assistantMessage("可以提问"));
    const user = userEvent.setup();
    const file = new File(["bytes"], "图.png", { type: "image/png" });
    await user.type(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), "找出处");
    await user.upload(screen.getByLabelText("选择聊天图片"), file);
    expect(screen.getByText("上传中")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    await act(async () => rejectUpload(new Error("上传连接中断")));
    expect(screen.getByRole("alert")).toHaveTextContent("上传连接中断");
    expect(screen.getByRole("button", { name: "发送问题" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "重试上传 图.png" }));
    expect(upload).toHaveBeenLastCalledWith("conversation-1", file, expect.any(AbortSignal));
    expect(screen.getByRole("button", { name: "发送问题" })).toBeEnabled();
    expect(mocks.send).not.toHaveBeenCalled();
  });

  it("only clears the submitted attachments when acceptance arrives after further editing", async () => {
    vi.spyOn(api, "uploadAgentAttachment").mockResolvedValue(uploaded);
    let accept!: () => void;
    mocks.send.mockImplementation((_text, _scope, _reference, _options, _context, submission) => { accept = submission.onAccepted; });
    renderPage(assistantMessage("可以提问"));
    const user = userEvent.setup();
    const textbox = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });
    await user.upload(screen.getByLabelText("选择聊天图片"), new File(["first"], "第一张.png", { type: "image/png" }));
    await user.type(textbox, "找第一张");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(textbox).toHaveValue("找第一张");
    await user.clear(textbox);
    await user.type(textbox, "下一条问题");
    await user.upload(screen.getByLabelText("选择聊天图片"), new File(["next"], "第二张.png", { type: "image/png" }));
    act(() => accept());
    expect(textbox).toHaveValue("下一条问题");
    expect(screen.queryByRole("img", { name: "第一张.png" })).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "第二张.png" })).toBeInTheDocument();
  });

  it("pastes image files through the upload path and leaves text paste and composition alone", async () => {
    const upload = vi.spyOn(api, "uploadAgentAttachment").mockResolvedValue(uploaded);
    renderPage(assistantMessage("可以提问"));
    const user = userEvent.setup();
    const textbox = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });
    await user.click(textbox);
    await user.paste("普通文字");
    expect(textbox).toHaveValue("普通文字");
    expect(upload).not.toHaveBeenCalled();
    const file = new File(["clip"], "粘贴.png", { type: "image/png" });
    fireEvent.paste(textbox, { clipboardData: { files: [file], getData: () => "" } });
    expect(await screen.findByText("已上传")).toBeInTheDocument();
    expect(upload).toHaveBeenCalledWith("conversation-1", file, expect.any(AbortSignal));
    fireEvent.keyDown(textbox, { key: "Enter", isComposing: true });
    expect(mocks.send).not.toHaveBeenCalled();
    await user.keyboard("{Shift>}{Enter}{/Shift}");
    expect(textbox).toHaveValue("普通文字\n");
    expect(mocks.send).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(mocks.send).toHaveBeenCalledOnce();
  });

  it("renders historical images in user messages and degrades missing metadata or failed previews", () => {
    const message = { ...assistantMessage("这张图出自哪里？"), role: "USER" as const, attachments: [
      uploaded,
      { mediaUri: "media://missing", mediaType: null, sizeBytes: null, previewUrl: null },
    ] };
    renderPage(message);
    expect(screen.getByRole("link", { name: "查看图片 1原图" })).toHaveAttribute("href", api.mediaUrl(uploaded.previewUrl));
    expect(screen.getByRole("img", { name: "图片 2：附件暂不可用" })).toBeInTheDocument();
    fireEvent.error(screen.getByRole("img", { name: "图片 1" }));
    expect(screen.getByRole("img", { name: "图片 1：附件暂不可用" })).toBeInTheDocument();
    expect(screen.getByText("这张图出自哪里？")).toBeInTheDocument();
  });
});
