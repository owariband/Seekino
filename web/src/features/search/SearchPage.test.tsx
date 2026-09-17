import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentMessageView, SeriesSummary, VideoChunkSearchHit } from "../../api/contracts";
import { SearchPage } from "./SearchPage";

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
  const startMs = 12_000 + (rank - 1) * 10_000;
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
      matchedEvidence: [{ kind: "TRANSCRIPT", text: "我们在门口等你。", startMs, endMs: startMs + 2_000 }],
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
      durationMs: 60_000,
      videoChunkId,
      sequenceNo: rank - 1,
      startMs,
      endMs: startMs + 3_000,
      analysisWindowStartMs: startMs - 2_000,
      analysisWindowEndMs: startMs + 6_000,
      transcript: [{ speaker: "角色甲", text: "我们在门口等你。", startMs, endMs: startMs + 2_000 }],
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
        atMs: startMs,
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
    replyToMessageId: "user-1",
    retrievalQuery: "雨天门口",
    searchResult: hits.length ? {
      query: "雨天门口",
      seriesIds: [series.seriesId],
      searchedVideoIds: ["video-1"],
      partial: false,
      warnings: [],
      hits,
    } : null,
    lastError: null,
    createdAt: now,
    updatedAt: now,
  };
}

function renderPage(message: AgentMessageView) {
  mocks.useWorkspace.mockReturnValue({ mode: "live", series: [series] });
  mocks.useAgentConversation.mockReturnValue({
    conversationId: "conversation-1",
    messages: [message],
    progress: {},
    loading: false,
    searching: false,
    error: null,
    send: mocks.send,
  });
  return render(<SearchPage />);
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
        { type: "video" as const, videoChunkId: "chunk-2" },
        { type: "markdown" as const, markdown: "两段之间的解释" },
        { type: "video" as const, videoChunkId: "unknown-chunk" },
        { type: "video" as const, videoChunkId: "chunk-1" },
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
