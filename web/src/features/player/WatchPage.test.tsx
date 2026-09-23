import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import type { SeriesView, VideoSummary } from "../../api/contracts";
import { WatchPage } from "./WatchPage";

const mocks = vi.hoisted(() => ({
  getSeries: vi.fn(),
  pauseAndGetSnapshot: vi.fn(),
  focusPlayer: vi.fn(),
  resumePlayer: vi.fn(),
  focusComposer: vi.fn(),
  receiveExtraContext: vi.fn(),
}));
vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  getSeries: mocks.getSeries,
}));
vi.mock("./VideoPlayer", async () => {
  const { forwardRef, useImperativeHandle, useRef } = await import("react");
  return {
    VideoPlayer: forwardRef(function MockVideoPlayer(
      { video, atSeconds, fullscreen, onToggleFullscreen, onPlaybackPositionChange }: {
        video: VideoSummary;
        atSeconds?: number;
        fullscreen?: boolean;
        onToggleFullscreen?: () => void;
        onPlaybackPositionChange?: (positionSeconds: number) => void;
      },
      ref,
    ) {
      const playerRef = useRef<HTMLDivElement>(null);
      useImperativeHandle(ref, () => ({
        pauseAndGetSnapshot: mocks.pauseAndGetSnapshot,
        focus: () => { playerRef.current?.focus(); mocks.focusPlayer(); },
        resume: mocks.resumePlayer,
      }));
      return (
        <div ref={playerRef} data-testid="video-player" tabIndex={0}>{video.videoId} @ {atSeconds === undefined ? "history" : atSeconds}
          <button type="button" onClick={onToggleFullscreen}>{fullscreen ? "退出网页全屏" : "网页全屏"}</button>
          <button type="button" onClick={() => onPlaybackPositionChange?.(atSeconds ?? 45)}>同步播放位置</button>
        </div>
      );
    }),
  };
});
vi.mock("../search/SearchPage", async () => {
  const { forwardRef, useImperativeHandle, useRef } = await import("react");
  return {
    SearchPage: forwardRef(function MockSearchPage({ embedded, initialSeriesId, captureExtraContext, onOpenVideo }: {
      embedded?: boolean;
      initialSeriesId?: string;
      captureExtraContext?: () => Record<string, unknown> | null;
      onOpenVideo?: (target: { seriesId: string; videoId: string; startSeconds: number }) => void;
    }, ref) {
      const composerRef = useRef<HTMLTextAreaElement>(null);
      useImperativeHandle(ref, () => ({
        focusComposer: () => { composerRef.current?.focus(); mocks.focusComposer(); },
      }));
      return (
        <div data-testid="supervisor-chat" data-embedded={embedded} data-series-id={initialSeriesId}>
          <textarea ref={composerRef} aria-label="向 anivag 助手提问" />
          <button type="button" onClick={() => onOpenVideo?.({ seriesId: "series-1", videoId: "video-1", startSeconds: 4 })}>打开视频证据</button>
          <button type="button" onClick={() => mocks.receiveExtraContext(captureExtraContext?.())}>捕获播放上下文</button>
        </div>
      );
    }),
  };
});

const video = (videoId: string, episodeOrder: number): VideoSummary => ({
  videoId,
  workspaceId: "local-default",
  seriesId: "series-1",
  episodeOrder,
  uploadSequence: episodeOrder,
  originalFilename: `${videoId}.mp4`,
  totalBytes: 100,
  durationSeconds: 120,
  uploadStatus: "COMPLETED",
  parseStatus: "SUCCEEDED",
  uploadProgress: 100,
  parseProgress: 100,
  indexStatus: "READY",
  playbackStatus: "READY",
  playbackUrl: `/api/videos/content?videoId=${videoId}&workspaceId=local-default&variant=playback`,
  playbackUrlExpiresAt: null,
  thumbnail: null,
  latestJobId: null,
  lastError: null,
});

const series: SeriesView = {
  seriesId: "series-1",
  workspaceId: "local-default",
  name: "测试课程",
  description: null,
  status: "READY",
  videoCount: 3,
  readyVideoCount: 3,
  failedVideoCount: 0,
  pendingUploadCount: 0,
  cover: null,
  lastSearch: null,
  createdAt: 0,
  updatedAt: 0,
  transcriptLanguageHint: null,
  importantExtraData: null,
  characterBible: [],
  videos: [video("video-3", 3), video("video-1", 1), video("video-2", 2)],
  pendingUploads: [],
};

function CurrentLocation() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

function renderPage(path = "/watch/series-1/video-2") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/watch/:seriesId/:videoId" element={<><WatchPage /><CurrentLocation /></>} />
          <Route path="/library" element={<p>资料库页面</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  mocks.getSeries.mockResolvedValue(series);
  mocks.pauseAndGetSnapshot.mockReturnValue({ positionSeconds: 12, wasPlaying: false });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("WatchPage", () => {
  it("starts with the Chat panel closed on a narrow screen so the video stays visible", async () => {
    const widthMock = vi.spyOn(window, "innerWidth", "get").mockReturnValue(600);
    renderPage();
    await screen.findByTestId("video-player");
    expect(document.getElementById("watch-side-panel")).toHaveAttribute("hidden");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    expect(document.getElementById("watch-side-panel")).not.toHaveAttribute("hidden");
    widthMock.mockRestore();
  });

  it("shows the whole episode beside the current series Supervisor Chat and keeps Chat mounted when collapsed", async () => {
    renderPage();
    expect(await screen.findByTestId("video-player")).toHaveTextContent("video-2 @ history");
    expect(screen.getByTestId("supervisor-chat")).toHaveAttribute("data-embedded", "true");
    expect(screen.getByTestId("supervisor-chat")).toHaveAttribute("data-series-id", "series-1");
    fireEvent.click(screen.getByRole("button", { name: "收起观看侧栏" }));
    expect(document.getElementById("watch-side-panel")).toHaveAttribute("hidden");
    expect(screen.getByTestId("supervisor-chat")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    expect(screen.getByRole("tab", { name: "Chat" })).toHaveAttribute("aria-selected", "true");
  });

  it("pauses the video when the user opens, selects, or interacts with Chat", async () => {
    renderPage();
    await screen.findByTestId("video-player");

    fireEvent.click(screen.getByRole("button", { name: "收起观看侧栏" }));
    expect(mocks.pauseAndGetSnapshot).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    expect(mocks.pauseAndGetSnapshot).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("tab", { name: "选集" }));
    fireEvent.click(screen.getByRole("tab", { name: "Chat" }));
    expect(mocks.pauseAndGetSnapshot).toHaveBeenCalledTimes(2);

    fireEvent.pointerDown(screen.getByTestId("supervisor-chat"), { button: 0, pointerType: "mouse" });
    expect(mocks.pauseAndGetSnapshot).toHaveBeenCalledTimes(3);

    fireEvent.pointerDown(screen.getByTestId("supervisor-chat"), { button: 2, pointerType: "mouse" });
    expect(mocks.pauseAndGetSnapshot).toHaveBeenCalledTimes(3);
  });

  it("canonicalizes a legacy atMs URL, sorts episodes, and leaves the old seek time behind", async () => {
    renderPage("/watch/series-1/video-2?atMs=13250");
    expect(await screen.findByTestId("video-player")).toHaveTextContent("video-2 @ 13");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/watch/series-1/video-2?atSeconds=13"));
    expect(screen.getByTestId("location")).not.toHaveTextContent("atMs");
    fireEvent.click(screen.getByRole("tab", { name: "选集" }));
    const panel = screen.getByRole("tabpanel", { name: "选集" });
    expect(within(panel).getByRole("link", { name: "调整集数" })).toHaveAttribute("href", "/library");
    expect(within(panel).getAllByRole("link").slice(1).map((item) => item.textContent?.slice(0, 5))).toEqual(["EP 01", "EP 02", "EP 03"]);
    fireEvent.click(screen.getByRole("link", { name: "下一集" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/watch/series-1/video-3"));
    expect(screen.getByTestId("video-player")).toHaveTextContent("video-3 @ history");
    expect(screen.getByTestId("location")).not.toHaveTextContent("atSeconds");
  });

  it("opens Chat video evidence at its exact timestamp", async () => {
    renderPage();
    await screen.findByTestId("supervisor-chat");
    fireEvent.click(screen.getByRole("button", { name: "打开视频证据" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/watch/series-1/video-1?atSeconds=4"));
    expect(screen.getByTestId("video-player")).toHaveTextContent("video-1 @ 4");
  });

  it("provides the current episode and exact paused position as generic extra context", async () => {
    renderPage();
    await screen.findByTestId("supervisor-chat");
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.pauseAndGetSnapshot).toHaveBeenCalledOnce();
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-2",
        episodeOrder: 2,
        positionSeconds: 12,
      },
    });

    fireEvent.click(screen.getByRole("link", { name: "下一集" }));
    await waitFor(() => expect(screen.getByTestId("video-player")).toHaveTextContent("video-3 @ history"));
    mocks.pauseAndGetSnapshot.mockReturnValue({ positionSeconds: 7, wasPlaying: false });
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-3",
        episodeOrder: 3,
        positionSeconds: 7,
      },
    });
  });

  it("keeps the latest reported playback context and does not reuse it after switching episodes", async () => {
    renderPage();
    await screen.findByTestId("video-player");
    mocks.pauseAndGetSnapshot.mockReturnValue(null);

    fireEvent.click(screen.getByRole("button", { name: "同步播放位置" }));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-2",
        episodeOrder: 2,
        positionSeconds: 45,
      },
    });

    fireEvent.click(screen.getByRole("link", { name: "下一集" }));
    await waitFor(() => expect(screen.getByTestId("video-player")).toHaveTextContent("video-3 @ history"));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith(null);

    fireEvent.click(screen.getByRole("button", { name: "同步播放位置" }));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-3",
        episodeOrder: 3,
        positionSeconds: 45,
      },
    });
  });

  it("discards the old position when the same video opens at a new timestamp", async () => {
    renderPage("/watch/series-1/video-1");
    await screen.findByTestId("video-player");
    mocks.pauseAndGetSnapshot.mockReturnValue(null);

    fireEvent.click(screen.getByRole("button", { name: "同步播放位置" }));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-1",
        episodeOrder: 1,
        positionSeconds: 45,
      },
    });

    fireEvent.click(screen.getByRole("button", { name: "打开视频证据" }));
    await waitFor(() => expect(screen.getByTestId("video-player")).toHaveTextContent("video-1 @ 4"));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith(null);

    fireEvent.click(screen.getByRole("button", { name: "同步播放位置" }));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-1",
        episodeOrder: 1,
        positionSeconds: 4,
      },
    });
  });

  it("switches focus between the viewer and Chat with Tab and resumes only an interrupted video", async () => {
    renderPage();
    const player = await screen.findByTestId("video-player");
    const composer = screen.getByRole("textbox", { name: "向 anivag 助手提问" });
    mocks.pauseAndGetSnapshot.mockReturnValue({ positionSeconds: 18, wasPlaying: true });

    fireEvent.click(screen.getByRole("tab", { name: "选集" }));
    fireEvent.click(screen.getByRole("button", { name: "收起观看侧栏" }));
    player.focus();
    fireEvent.keyDown(player, { key: "Tab" });

    expect(mocks.pauseAndGetSnapshot).toHaveBeenCalledOnce();
    expect(screen.getByRole("complementary", { name: "观看侧栏" })).not.toHaveAttribute("hidden");
    expect(screen.getByRole("tab", { name: "Chat" })).toHaveAttribute("aria-selected", "true");
    expect(composer).toHaveFocus();
    expect(mocks.focusComposer).toHaveBeenCalledOnce();

    mocks.pauseAndGetSnapshot.mockReturnValue({ positionSeconds: 18, wasPlaying: false });
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    fireEvent.keyDown(composer, { key: "Tab", shiftKey: true });
    expect(player).toHaveFocus();
    expect(mocks.focusPlayer).toHaveBeenCalledOnce();
    expect(mocks.resumePlayer).toHaveBeenCalledOnce();

    fireEvent.keyDown(player, { key: "Tab", shiftKey: true });
    expect(composer).toHaveFocus();
    fireEvent.keyDown(composer, { key: "Tab" });
    expect(mocks.resumePlayer).toHaveBeenCalledOnce();
  });

  it("makes the whole watch page fullscreen so Chat stays beside the video", async () => {
    renderPage();
    await screen.findByTestId("video-player");
    const page = screen.getByRole("main");
    const requestFullscreen = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(page, "requestFullscreen", { value: requestFullscreen });
    fireEvent.click(screen.getByRole("button", { name: "网页全屏" }));
    expect(requestFullscreen).toHaveBeenCalledOnce();
    expect(page).toContainElement(screen.getByTestId("supervisor-chat"));
    expect(page).toContainElement(screen.getByRole("complementary", { name: "观看侧栏" }));
  });

  it("resizes the Chat panel with the divider and saves its width", async () => {
    renderPage();
    const divider = await screen.findByRole("separator", { name: "调整右侧栏宽度" });
    const layout = divider.parentElement!;
    Object.defineProperty(layout, "clientWidth", { value: 1200 });
    Object.defineProperty(layout, "getBoundingClientRect", { value: () => ({ left: 0, right: 1200, width: 1200 }) });
    fireEvent.keyDown(divider, { key: "ArrowLeft" });
    expect(divider).toHaveAttribute("aria-valuenow", "464");
    fireEvent.pointerDown(divider, { pointerId: 1, clientX: 740 });
    fireEvent.pointerMove(divider, { pointerId: 1, clientX: 600 });
    fireEvent.pointerUp(divider, { pointerId: 1 });
    expect(divider).toHaveAttribute("aria-valuenow", "600");
    expect(layout.style.getPropertyValue("--watch-panel-width")).toBe("600px");
    expect(localStorage.getItem("anivag-watch-panel-width")).toBe("600");
  });
});
