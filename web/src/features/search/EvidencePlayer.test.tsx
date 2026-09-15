import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { VideoChunkSearchHit } from "../../api/contracts";
import { EvidencePlayer } from "./EvidencePlayer";

const hit: VideoChunkSearchHit = {
  candidate: {
    videoChunkId: "chunk-1",
    rank: 1,
    fusedRank: 1,
    fusedScore: 0.032,
    textRank: 1,
    textScore: 0.91,
    visionRank: 2,
    visionScore: 0.84,
  },
  selectionReason: {
    summary: "对白中的等待与画面中的雨伞在同一时间片出现，因此优先核验。",
    matchedEvidence: [
      {
        kind: "TRANSCRIPT",
        text: "我们在门口等你。",
        startMs: 12_000,
        endMs: 14_000,
      },
      {
        kind: "VISUAL_EVENT",
        text: "人物手持雨伞站在门口。",
        startMs: 12_000,
        endMs: 15_000,
      },
    ],
    limitations: [],
  },
  evidence: {
    seriesId: "series-1",
    workspaceId: "local-default",
    seriesName: "测试系列",
    seriesStatus: "READY",
    videoId: "video-1",
    episodeOrder: 1,
    originalFilename: "episode-01.mp4",
    durationMs: 60_000,
    videoChunkId: "chunk-1",
    sequenceNo: 0,
    startMs: 12_000,
    endMs: 15_000,
    analysisWindowStartMs: 10_000,
    analysisWindowEndMs: 18_000,
    transcript: [
      { speaker: "角色甲", text: "我们在门口等你。", startMs: 12_000, endMs: 14_000 },
    ],
    baselineVisualCaption: "人物手持雨伞站在门口。",
    allTimeEvents: [],
    allTimeEventText: "人物手持雨伞站在门口。",
    videoCompact: "雨天，人物在门口等待。",
    contextBeforeCompact: "室外开始下雨。",
    contextAfterCompact: "另一人从走廊出现。",
    windowSummary: "一场发生在门口的等待。",
    modelProvider: "provider",
    modelName: "model",
    pipelineVersion: "m1",
    isFallback: false,
    parsedAt: Date.parse("2026-09-15T00:00:00Z"),
    playbackStatus: "READY",
    playbackUrl: "/api/videos/content?videoId=video-1&workspaceId=local-default&variant=playback",
    playbackUrlExpiresAt: null, thumbnail: null, keyframes: [],
  },
};

let pause: ReturnType<typeof vi.spyOn>;
let play: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function metadata(video: HTMLVideoElement) {
  Object.defineProperty(video, "readyState", { value: HTMLMediaElement.HAVE_METADATA, configurable: true });
  fireEvent.loadedMetadata(video);
}
function nextHit(overrides: Partial<VideoChunkSearchHit["evidence"]>): VideoChunkSearchHit {
  return { ...hit, evidence: { ...hit.evidence, ...(overrides.videoId ? { playbackUrl: `/api/videos/content?videoId=${overrides.videoId}&workspaceId=local-default&variant=playback` } : {}), ...overrides } };
}

describe("Evidence range playback", () => {
  it("waits for metadata, then seeks to the selected range and honours an early play request", () => {
    render(<EvidencePlayer hit={hit} />);
    const video = screen.getByLabelText<HTMLVideoElement>("视频证据");
    expect(video.currentTime).toBe(0);
    expect(video.src).toContain("/api/videos/content?videoId=video-1&workspaceId=local-default");
    fireEvent.click(screen.getByRole("button", { name: /从 .* 播放/ }));
    expect(play).not.toHaveBeenCalled();
    metadata(video);
    expect(video.currentTime).toBe(12);
    expect(play).toHaveBeenCalledOnce();
  });

  it("pauses at endMs and restarts native playback inside the selected range", () => {
    render(<EvidencePlayer hit={hit} />);
    const video = screen.getByLabelText<HTMLVideoElement>("视频证据");
    metadata(video);
    pause.mockClear();
    video.currentTime = 14.9; fireEvent.timeUpdate(video);
    expect(pause).not.toHaveBeenCalled();
    video.currentTime = 15.4; fireEvent.timeUpdate(video);
    expect(video.currentTime).toBe(15);
    expect(pause).toHaveBeenCalledOnce();
    video.currentTime = 0; fireEvent.play(video);
    expect(video.currentTime).toBe(12);
  });

  it("replaces the range on the same video and preserves playback during equivalent snapshots", () => {
    const view = render(<EvidencePlayer hit={hit} />);
    const video = screen.getByLabelText<HTMLVideoElement>("视频证据");
    metadata(video);
    video.currentTime = 13.2;
    pause.mockClear();
    view.rerender(<EvidencePlayer hit={nextHit({})} />);
    expect(video.currentTime).toBe(13.2);
    expect(pause).not.toHaveBeenCalled();
    view.rerender(<EvidencePlayer hit={nextHit({ videoChunkId: "chunk-2", startMs: 25000, endMs: 28000 })} />);
    expect(screen.getByLabelText("视频证据")).toBe(video);
    expect(video.currentTime).toBe(25);
    pause.mockClear();
    video.currentTime = 26; fireEvent.timeUpdate(video);
    expect(pause).not.toHaveBeenCalled();
    video.currentTime = 28.5; fireEvent.timeUpdate(video);
    expect(video.currentTime).toBe(28);
    expect(pause).toHaveBeenCalledOnce();
  });

  it("removes old metadata and range listeners when the video source changes", () => {
    const view = render(<EvidencePlayer hit={hit} />);
    const oldVideo = screen.getByLabelText<HTMLVideoElement>("视频证据");
    metadata(oldVideo);
    view.rerender(<EvidencePlayer hit={nextHit({ videoId: "video-2", videoChunkId: "chunk-2", startMs: 25000, endMs: 28000 })} />);
    const video = screen.getByLabelText<HTMLVideoElement>("视频证据");
    expect(video).not.toBe(oldVideo);
    oldVideo.currentTime = 0;
    pause.mockClear();
    fireEvent.loadedMetadata(oldVideo);
    oldVideo.currentTime = 16; fireEvent.timeUpdate(oldVideo);
    expect(oldVideo.currentTime).toBe(16);
    expect(pause).not.toHaveBeenCalled();
    expect(video.currentTime).toBe(0);
    metadata(video);
    expect(video.currentTime).toBe(25);
  });

  it("renders play rejection and ignores a rejected Promise from an old range", async () => {
    const view = render(<EvidencePlayer hit={hit} />);
    const video = screen.getByLabelText<HTMLVideoElement>("视频证据");
    metadata(video);
    play.mockRejectedValueOnce(new Error("浏览器拒绝播放"));
    fireEvent.click(screen.getByRole("button", { name: /从 .* 播放/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("浏览器拒绝播放");
    let reject!: (error: Error) => void;
    play.mockImplementationOnce(() => new Promise<void>((_resolve, fail) => { reject = fail; }));
    fireEvent.click(screen.getByRole("button", { name: /从 .* 播放/ }));
    view.rerender(<EvidencePlayer hit={nextHit({ videoChunkId: "chunk-2", startMs: 25000, endMs: 28000 })} />);
    reject(new Error("旧片段的错误"));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("retains textual evidence and disables playback when the original media is missing", () => {
    render(<EvidencePlayer hit={nextHit({ playbackStatus: "MISSING", playbackUrl: null })} />);
    expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("原视频文件已丢失");
    expect(screen.getByRole("button", { name: /从 .* 播放/ })).toBeDisabled();
    expect(screen.getByText(hit.selectionReason.summary)).toBeInTheDocument();
  });

  it("refreshes a failed media URL from the read-only scoped Evidence endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...hit.evidence, playbackStatus: "MISSING", playbackUrl: null }), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<EvidencePlayer hit={hit} />);
    fireEvent.error(screen.getByLabelText("视频证据"));
    expect(screen.getByRole("alert")).toHaveTextContent("视频媒体无法读取");
    fireEvent.click(screen.getByRole("button", { name: "刷新媒体状态" }));
    await waitFor(() => expect(screen.queryByLabelText("视频证据")).not.toBeInTheDocument());
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("/api/video-chunks/get?workspaceId=local-default&videoChunkId=chunk-1");
    expect(screen.getByRole("status")).toHaveTextContent("原视频文件已丢失");
    expect(screen.getByText(hit.selectionReason.summary)).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
