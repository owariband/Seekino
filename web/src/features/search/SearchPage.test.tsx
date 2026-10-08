import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentMessageView, RetrievalChannelWeights, SeriesSummary, VideoChunkSearchHit } from "../../api/contracts";
import { SearchPage, type SearchPageHandle } from "./SearchPage";

const mocks = vi.hoisted(() => ({
  send: vi.fn(),
  useAgentConversation: vi.fn(),
  useWorkspace: vi.fn(),
}));

vi.mock("../../app/workspace", () => ({ useWorkspace: mocks.useWorkspace }));
vi.mock("./useAgentConversation", () => ({ useAgentConversation: mocks.useAgentConversation }));

const now = Date.parse("2026-09-17T00:00:00Z");
const series: SeriesSummary = {
  seriesId: "series-1",
  workspaceId: "local-default",
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
      workspaceId: series.workspaceId,
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
      playbackUrl: "/api/videos/content?videoId=video-1&workspaceId=local-default&variant=playback",
      playbackUrlExpiresAt: null,
      thumbnail: {
        imageId: `thumbnail-${rank}`,
        url: `/api/media/images/content?workspaceId=local-default&imageId=thumbnail-${rank}`,
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
    captureExtraContext?: () => Record<string, unknown> | null;
    onOpenVideo?: (target: { seriesId: string; videoId: string; startSeconds: number }) => void;
  } = {},
) {
  mocks.useWorkspace.mockReturnValue({ mode: "live", series: videoSeries, isLoading: false });
  mocks.useAgentConversation.mockReturnValue({
    conversationId: "conversation-1",
    messages: [message],
    progress: {},
    loading: false,
    searching: false,
    error: null,
    send: mocks.send,
  });
  return render(<SearchPage {...props} />);
}

beforeEach(() => {
  mocks.send.mockReset();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Search assistant presentation", () => {
  it("uses the current ready series for an embedded conversation and opens evidence in the whole-video player", async () => {
    const hit = searchHit("chunk-1", 1, "片段首帧");
    const onOpenVideo = vi.fn();
    renderPage(assistantMessage("找到片段", [hit]), [series], {
      embedded: true, initialSeriesId: series.seriesId, onOpenVideo,
    });
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "当前对话", level: 2 })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "anivag 助手" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("打开对话记录")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建对话" })).toBeInTheDocument();
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent(series.name);
    expect(screen.getByRole("textbox", { name: "向 anivag 助手提问" }).closest(".page")).toBeNull();
    await user.type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "这段发生了什么？");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(mocks.send).toHaveBeenCalledWith("这段发生了什么？", [series.seriesId], null, {});

    await user.click(screen.getByRole("img", { name: "片段首帧" }));
    expect(onOpenVideo).toHaveBeenCalledWith({ seriesId: series.seriesId, videoId: "video-1", startSeconds: hit.evidence.startSeconds });
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
    const textbox = screen.getByRole("textbox", { name: "向 anivag 助手提问" });

    chatRef.current?.focusComposer();
    expect(textbox).toHaveFocus();
    expect(captureExtraContext).not.toHaveBeenCalled();

    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "这里继续讲了什么？", { skipClick: true });
    currentContext = extraContext;
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(captureExtraContext).toHaveBeenCalledOnce();
    expect(mocks.send).toHaveBeenCalledWith(
      "这里继续讲了什么？",
      [series.seriesId],
      null,
      {},
      extraContext,
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
    const textbox = screen.getByRole("textbox", { name: "向 anivag 助手提问" });
    textbox.focus();
    const user = userEvent.setup();
    await user.type(textbox, "开头讲了什么？", { skipClick: true });
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(mocks.send).not.toHaveBeenCalled();
    expect(textbox).toHaveValue("开头讲了什么？");

    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(captureExtraContext).toHaveBeenCalledTimes(2);
    expect(mocks.send).toHaveBeenCalledWith("开头讲了什么？", [series.seriesId], null, {}, extraContext);
  });

  it("keeps the question and does not send when playback context is unavailable on submit", async () => {
    const captureExtraContext = vi.fn(() => null);
    renderPage(assistantMessage("可以提问"), [series], {
      embedded: true,
      initialSeriesId: series.seriesId,
      captureExtraContext,
    });
    const user = userEvent.setup();
    const textbox = screen.getByRole("textbox", { name: "向 anivag 助手提问" });
    textbox.focus();
    await user.type(textbox, "这里讲了什么？", { skipClick: true });
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(captureExtraContext).toHaveBeenCalledOnce();
    expect(mocks.send).not.toHaveBeenCalled();
    expect(textbox).toHaveValue("这里讲了什么？");
    expect(screen.getByText("当前播放位置尚未就绪，请稍后重试")).toHaveAttribute("role", "alert");
  });

  it("states when the watched series is not searchable instead of presenting a global scope as current", async () => {
    renderPage(assistantMessage("可以继续聊天"), [{ ...series, status: "PARTIAL" }], {
      embedded: true, initialSeriesId: series.seriesId,
    });
    const user = userEvent.setup();

    expect(screen.getByText(/当前系列暂不可检索/)).toHaveTextContent("全部可检索系列");
    expect(screen.getByLabelText("选择检索视频系列")).toHaveTextContent("全部视频系列");
    await user.type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "你好");
    await user.click(screen.getByRole("button", { name: "发送问题" }));
    expect(mocks.send).toHaveBeenCalledWith("你好", [], null, {});
  });

  it("allows a conversation without ready videos and displays a web source link", async () => {
    renderPage({
      ...assistantMessage("[公开资料](https://example.com/source)"),
      seriesIds: [],
    }, []);
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "anivag 助手", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "公开资料" })).toHaveAttribute("href", "https://example.com/source");
    expect(screen.queryByText(/可用视频范围/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("回答过程")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "向 anivag 助手提问" }), "你好");
    await user.click(screen.getByRole("button", { name: "发送问题" }));

    expect(mocks.send).toHaveBeenCalledWith("你好", [], null, {});
    expect(screen.getByText("问题已发送，正在处理")).toBeInTheDocument();
  });

  const traceCases: Array<{ label: string; weights: RetrievalChannelWeights }> = [
    { label: "historical ASR", weights: { transcript: 0.2, semantic: 0.5, visual: 0.3 } },
    { label: "lexical", weights: { lexical: 0.2, semantic: 0.5, visual: 0.3 } },
  ];

  it.each(traceCases)("distinguishes retrieved candidates from an empty retrieval with $label trace", ({ weights }) => {
    const message = {
      ...assistantMessage("只生成了文字"),
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
      renderBlocks: [{ type: "markdown" as const, markdown: "只生成了文字" }],
    };

    renderPage(message);

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
    const secondThumbnail = screen.getByRole("img", { name: "第二段首帧" });
    const bridge = screen.getByText("两段之间的解释");
    const firstThumbnail = screen.getByRole("img", { name: "第一段首帧" });
    expect(introduction.compareDocumentPosition(secondThumbnail) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(secondThumbnail.compareDocumentPosition(bridge) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(bridge.compareDocumentPosition(firstThumbnail) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
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
    await user.click(screen.getByRole("img", { name: "精确片段首帧" }));
    const video = screen.getByLabelText<HTMLVideoElement>("视频证据");
    Object.defineProperty(video, "readyState", { value: HTMLMediaElement.HAVE_METADATA, configurable: true });
    video.dispatchEvent(new Event("loadedmetadata"));

    expect(video.currentTime).toBe(12);
    expect(screen.getByRole("slider", { name: "视频片段播放进度" })).toHaveAttribute("max", "1000");
    expect(screen.getByText("00:00 / 00:01")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "引用此片段追问" }));
    expect(screen.getByText(/已引用.*00:12–00:13/)).toBeInTheDocument();
  });

  it("drops an out-of-bounds approved range instead of expanding it to the canonical chunk", async () => {
    const hit = searchHit("chunk-1", 1, "越界片段首帧");
    const message = {
      ...assistantMessage("越界片段", [hit]),
      renderBlocks: [
        { type: "markdown" as const, markdown: "越界片段" },
        { type: "video" as const, videoChunkId: hit.evidence.videoChunkId, startSeconds: 11_000, endSeconds: 13_000 },
      ],
    };
    renderPage(message);

    expect(await screen.findByText("越界片段")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "越界片段首帧" })).not.toBeInTheDocument();
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
    expect(screen.queryByRole("img", { name: "旧消息首帧" })).not.toBeInTheDocument();
    expect(screen.queryByText("00:12–00:15")).not.toBeInTheDocument();
  });

  it("shows only thumbnail images until selection and keeps exactly one selected video mounted", async () => {
    const hits = [searchHit("chunk-1", 1, "第一段首帧"), searchHit("chunk-2", 2, "第二段首帧")];
    renderPage(assistantMessage("找到两个候选片段。", hits));
    const user = userEvent.setup();

    expect(screen.getByRole("heading", { name: "视频片段" })).toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(2);
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "第一段首帧" })).toHaveAttribute("loading", "lazy");

    await user.click(screen.getByRole("img", { name: "第一段首帧" }));
    const firstVideo = screen.getByLabelText<HTMLVideoElement>("视频证据");
    expect(screen.getAllByLabelText("视频证据")).toHaveLength(1);
    expect(firstVideo).toHaveAttribute("preload", "metadata");

    await user.click(screen.getByRole("img", { name: "第二段首帧" }));
    const secondVideo = screen.getByLabelText<HTMLVideoElement>("视频证据");
    expect(screen.getAllByLabelText("视频证据")).toHaveLength(1);
    expect(secondVideo).not.toBe(firstVideo);

    await user.click(screen.getByRole("button", { name: "关闭片段详情" }));
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();
    expect(screen.getAllByRole("img")).toHaveLength(2);
  });

  it("keeps a playable card understandable when a historical chunk has no thumbnail", async () => {
    const hit = searchHit("chunk-without-thumbnail", 1, "不会显示的首帧");
    hit.evidence.thumbnail = null;
    renderPage(assistantMessage("找到一个可播放片段。", [hit]));
    const user = userEvent.setup();

    expect(screen.getByRole("img", { name: "该片段暂无首帧缩略图" })).toHaveTextContent("暂无首帧 · 点击加载视频");
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();

    await user.click(screen.getByRole("img", { name: "该片段暂无首帧缩略图" }));
    expect(screen.getByLabelText("视频证据")).toHaveAttribute("preload", "metadata");
  });
});
