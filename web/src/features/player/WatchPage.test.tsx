import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, Route, Routes, useLocation } from "react-router";

import type { SeriesSummary, SeriesView, VideoSummary } from "../../api/contracts";
import type { WatchPanelLayout } from "../../app/AppShell";
import { WatchPage } from "./WatchPage";

const mocks = vi.hoisted(() => ({
  getSeries: vi.fn(),
  getSnapshot: vi.fn(),
  pauseAndGetSnapshot: vi.fn(),
  focusPlayer: vi.fn(),
  resumePlayer: vi.fn(),
  focusComposer: vi.fn(),
  receiveExtraContext: vi.fn(),
  useAuth: vi.fn(),
}));
vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  getSeries: mocks.getSeries,
}));
vi.mock("./PlaybackFrame", () => ({
  PlaybackFrame: () => <span aria-hidden="true" />,
}));
vi.mock("../auth/AuthProvider", () => ({ useAuth: mocks.useAuth }));
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
        getSnapshot: mocks.getSnapshot,
        pauseAndGetSnapshot: mocks.pauseAndGetSnapshot,
        focus: () => { playerRef.current?.focus(); mocks.focusPlayer(); },
        resume: mocks.resumePlayer,
      }));
      return (
        <div ref={playerRef} data-testid="video-player" tabIndex={0}>{video.videoId} @ {atSeconds === undefined ? "history" : atSeconds}
          <button type="button" onClick={onToggleFullscreen}>{fullscreen ? "退出网页全屏" : "网页全屏"}</button>
          <button type="button" onClick={() => onPlaybackPositionChange?.(atSeconds ?? 45_000)}>同步播放位置</button>
        </div>
      );
    }),
  };
});
vi.mock("../search/SearchPage", async () => {
  const { forwardRef, useImperativeHandle, useRef } = await import("react");
  return {
    SearchPage: forwardRef(function MockSearchPage({ embedded, initialSeriesId, initialSeries, captureExtraContext, onOpenVideo }: {
      embedded?: boolean;
      initialSeriesId?: string;
      initialSeries?: SeriesSummary;
      captureExtraContext?: () => Record<string, unknown> | null;
      onOpenVideo?: (target: { tenantId: string; seriesId: string; videoId: string; startSeconds: number }) => void;
    }, ref) {
      const composerRef = useRef<HTMLTextAreaElement>(null);
      useImperativeHandle(ref, () => ({
        focusComposer: () => { composerRef.current?.focus(); mocks.focusComposer(); },
      }));
      return (
        <div data-testid="supervisor-chat" data-embedded={embedded} data-series-id={initialSeriesId} data-source-tenant={initialSeries?.tenantId}>
          <textarea ref={composerRef} aria-label="向 Seekino 助手提问" />
          <button type="button" onClick={() => onOpenVideo?.({ tenantId: initialSeries?.tenantId ?? "local-default", seriesId: "series-1", videoId: "video-1", startSeconds: 4_000 })}>打开视频证据</button>
          <button type="button" onClick={() => mocks.receiveExtraContext(captureExtraContext?.())}>捕获播放上下文</button>
        </div>
      );
    }),
  };
});

const video = (videoId: string, episodeOrder: number): VideoSummary => ({
  videoId,
  tenantId: "local-default",
  seriesId: "series-1",
  episodeOrder,
  uploadSequence: episodeOrder,
  originalFilename: `${videoId}.mp4`,
  totalBytes: 100,
  durationSeconds: 120_000,
  uploadStatus: "COMPLETED",
  parseStatus: "SUCCEEDED",
  uploadProgress: 100,
  parseProgress: 100,
  indexStatus: "READY",
  playbackStatus: "READY",
  playbackUrl: `/api/videos/content?videoId=${videoId}&tenantId=local-default&variant=playback`,
  playbackUrlExpiresAt: null,
  thumbnail: null,
  latestJobId: null,
  lastError: null,
});

const series: SeriesView = {
  visibility: "private", access: "member", publisher: null,
  seriesId: "series-1",
  tenantId: "local-default",
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

function WatchShell() {
  const [layout, setLayout] = useState<WatchPanelLayout>({ open: false, width: 440, resizing: false });
  return <div data-app-shell data-testid="watch-shell" data-watch-panel-open={layout.open}
    data-watch-panel-resizing={layout.resizing} data-watch-panel-width={layout.width}>
    <Outlet context={setLayout} />
    <div id="watch-side-panel-host" />
  </div>;
}

function renderPage(path = "/watch/series-1/video-2", withShell = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={withShell ? <WatchShell /> : <Outlet />}>
            <Route path="/watch/:seriesId/:videoId" element={<><WatchPage /><CurrentLocation /></>} />
            <Route path="/library" element={<p>资料库页面</p>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.useAuth.mockReturnValue({ canManage: true });
  localStorage.clear();
  mocks.getSeries.mockResolvedValue(series);
  mocks.getSnapshot.mockReturnValue({ positionSeconds: 12_000, wasPlaying: false });
  mocks.pauseAndGetSnapshot.mockReturnValue({ positionSeconds: 12_000, wasPlaying: false });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("WatchPage", () => {
  it("shows per-video title, synopsis and cover while keeping episode navigation", async () => {
    mocks.getSeries.mockResolvedValue({
      ...series, description: "系列简介",
      videos: series.videos.map((episode) => ({ ...episode,
        displayName: episode.videoId === "video-2" ? "本集标题" : "后续集标题",
        description: episode.videoId === "video-2" ? "本集内容简介" : null,
        thumbnail: { imageId: "cover", url: "/api/media/images/content?imageId=cover", expiresAt: null },
      })),
    });
    renderPage();
    expect(await screen.findByRole("heading", { level: 1, name: "本集标题" })).toBeInTheDocument();
    expect(screen.getByText("本集内容简介")).toBeInTheDocument();
    expect(screen.queryByText("系列简介")).not.toBeInTheDocument();
    const next = screen.getByRole("region", { name: "后续集数" });
    expect(within(next).getByText("后续集标题")).toBeInTheDocument();
    expect(next.querySelector("img")).toHaveAttribute("src", expect.stringContaining("imageId=cover"));
    fireEvent.click(screen.getByRole("link", { name: "下一集" }));
    await waitFor(() => expect(screen.getByTestId("video-player")).toHaveTextContent("video-3"));
  });

  it("hides the library shortcut when viewing one's own series from a public source", async () => {
    mocks.useAuth.mockReturnValue({ canManage: false });
    renderPage();
    await screen.findByTestId("video-player");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    fireEvent.click(screen.getByRole("tab", { name: "选集" }));
    expect(screen.queryByRole("link", { name: "调整集数" })).not.toBeInTheDocument();
  });

  it("preserves the source tenant through a public deep link, seek, and episode navigation", async () => {
    mocks.getSeries.mockResolvedValue({ ...series, tenantId: "tenant-alice", visibility: "public", access: "public" });
    renderPage("/watch/series-1/video-2?tenantId=tenant-alice&atMs=13250");
    expect(await screen.findByTestId("video-player")).toHaveTextContent("video-2 @ 13250");
    expect(mocks.getSeries).toHaveBeenCalledWith("series-1", "tenant-alice");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("tenantId=tenant-alice&atSeconds=13250"));
    expect(screen.getByTestId("supervisor-chat")).toHaveAttribute("data-source-tenant", "tenant-alice");
    expect(screen.getByRole("link", { name: "测试课程" })).toHaveAttribute("href", "/series/series-1?tenantId=tenant-alice");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    fireEvent.click(screen.getByRole("button", { name: "打开视频证据" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/watch/series-1/video-1?tenantId=tenant-alice&atSeconds=4000"));
    fireEvent.click(screen.getByRole("tab", { name: "选集" }));
    expect(screen.queryByRole("link", { name: "调整集数" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "下一集" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/watch/series-1/video-2?tenantId=tenant-alice"));
    expect(screen.getByTestId("location")).not.toHaveTextContent("atSeconds");
  });

  it.each([600, 1440])("starts with Chat closed at %s px so the video stays unobstructed", async (width) => {
    const widthMock = vi.spyOn(window, "innerWidth", "get").mockReturnValue(width);
    renderPage();
    await screen.findByTestId("video-player");
    expect(document.getElementById("watch-side-panel")).toHaveAttribute("aria-hidden", "true");
    expect(document.getElementById("watch-side-panel")).toHaveAttribute("inert");
    const toggle = screen.getByRole("button", { name: "打开观看侧栏" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(mocks.pauseAndGetSnapshot).not.toHaveBeenCalled();
    fireEvent.click(toggle);
    expect(document.getElementById("watch-side-panel")).toHaveAttribute("aria-hidden", "false");
    expect(document.getElementById("watch-side-panel")).not.toHaveAttribute("inert");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    widthMock.mockRestore();
  });

  it("keeps both the current episode and series Chat mounted while opening and closing the sidebar", async () => {
    renderPage();
    const player = await screen.findByTestId("video-player");
    const chat = screen.getByTestId("supervisor-chat");
    expect(player).toHaveTextContent("video-2 @ history");
    expect(chat).toHaveAttribute("data-embedded", "true");
    expect(chat).toHaveAttribute("data-series-id", "series-1");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    fireEvent.click(screen.getByRole("button", { name: "收起观看侧栏" }));
    expect(document.getElementById("watch-side-panel")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByTestId("supervisor-chat")).toBe(chat);
    expect(screen.getByTestId("video-player")).toBe(player);
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    expect(screen.getByRole("tab", { name: "聊天" })).toHaveAttribute("aria-selected", "true");
  });

  it("keeps playback unchanged when the user opens, selects, or interacts with Chat", async () => {
    renderPage();
    await screen.findByTestId("video-player");

    expect(mocks.pauseAndGetSnapshot).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    fireEvent.click(screen.getByRole("button", { name: "收起观看侧栏" }));
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));

    fireEvent.click(screen.getByRole("tab", { name: "选集" }));
    fireEvent.click(screen.getByRole("tab", { name: "聊天" }));

    fireEvent.pointerDown(screen.getByTestId("supervisor-chat"), { button: 0, pointerType: "mouse" });
    fireEvent.pointerDown(screen.getByTestId("supervisor-chat"), { button: 2, pointerType: "mouse" });
    fireEvent.pointerDown(screen.getByRole("textbox", { name: "向 Seekino 助手提问" }), { button: 0, pointerType: "touch" });
    expect(mocks.pauseAndGetSnapshot).not.toHaveBeenCalled();
    expect(mocks.getSnapshot).not.toHaveBeenCalled();
  });

  it("shows the episode description and ordered upcoming episodes while Chat is closed", async () => {
    mocks.getSeries.mockResolvedValue({ ...series, description: "一起寻找故事里的名场面。" });
    renderPage("/watch/series-1/video-1?atSeconds=12000");
    const description = await screen.findByRole("region", { name: "视频简介" });
    expect(within(description).getByRole("link", { name: "测试课程" })).toHaveAttribute("href", "/series/series-1?tenantId=local-default");
    expect(within(description).getByRole("heading", { level: 1 })).toHaveTextContent("第 1 集");
    expect(within(description).getByRole("heading", { level: 1 })).toHaveAttribute("title", "video-1.mp4");
    expect(within(description).getByText("一起寻找故事里的名场面。")).toBeVisible();
    const upcoming = screen.getByRole("region", { name: "后续集数" });
    expect(within(upcoming).getByRole("heading", { name: "接下来" })).toBeVisible();
    const links = within(upcoming).getAllByRole("link");
    expect(links.map((item) => item.getAttribute("href"))).toEqual([
      "/watch/series-1/video-2?tenantId=local-default", "/watch/series-1/video-3?tenantId=local-default",
    ]);
    expect(document.getElementById("watch-side-panel")).toHaveAttribute("aria-hidden", "true");
    fireEvent.click(links[0]!);
    await waitFor(() => expect(screen.getByTestId("video-player")).toHaveTextContent("video-2 @ history"));
    expect(screen.getByTestId("location")).not.toHaveTextContent("atSeconds");
  });

  it("offers the full series when there are no later episodes", async () => {
    renderPage("/watch/series-1/video-3");
    const upcoming = await screen.findByRole("region", { name: "后续集数" });
    expect(within(upcoming).getByRole("heading", { name: "本系列选集" })).toBeVisible();
    expect(within(upcoming).getAllByRole("link").map((item) => item.getAttribute("href"))).toEqual([
      "/watch/series-1/video-1?tenantId=local-default", "/watch/series-1/video-2?tenantId=local-default", "/watch/series-1/video-3?tenantId=local-default",
    ]);
    expect(screen.queryByRole("link", { name: "下一集" })).not.toBeInTheDocument();
  });

  it("returns to the player for a new episode or timestamp without scrolling when Chat toggles", async () => {
    const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView });
    try {
      renderPage("/watch/series-1/video-1");
      await screen.findByTestId("video-player");
      expect(scrollIntoView).toHaveBeenCalledOnce();
      expect(scrollIntoView).toHaveBeenLastCalledWith({ block: "start", behavior: "instant" });
      expect(scrollIntoView.mock.contexts[0]).toBe(screen.getByRole("region", { name: "视频播放器" }).parentElement);

      fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
      fireEvent.click(screen.getByRole("button", { name: "收起观看侧栏" }));
      expect(scrollIntoView).toHaveBeenCalledOnce();
      fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
      fireEvent.click(screen.getByRole("button", { name: "打开视频证据" }));
      await waitFor(() => expect(screen.getByTestId("video-player")).toHaveTextContent("video-1 @ 4000"));
      expect(scrollIntoView).toHaveBeenCalledTimes(2);

      fireEvent.click(screen.getByRole("link", { name: "下一集" }));
      await waitFor(() => expect(screen.getByTestId("video-player")).toHaveTextContent("video-2 @ history"));
      expect(scrollIntoView).toHaveBeenCalledTimes(3);
    } finally {
      if (originalScroll) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScroll);
      else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
    }
  });

  it("closes Chat with Escape and restores focus to its toggle", async () => {
    renderPage();
    await screen.findByTestId("video-player");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    const composer = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });
    expect(composer).toHaveFocus();
    fireEvent.keyDown(composer, { key: "Escape" });
    expect(document.getElementById("watch-side-panel")).toHaveAttribute("aria-hidden", "true");
    expect(document.getElementById("watch-side-panel")).toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "打开观看侧栏" })).toHaveFocus();
  });

  it("canonicalizes a legacy atMs URL, sorts episodes, and leaves the old seek time behind", async () => {
    renderPage("/watch/series-1/video-2?atMs=13250");
    expect(await screen.findByTestId("video-player")).toHaveTextContent("video-2 @ 13250");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/watch/series-1/video-2?atSeconds=13250"));
    expect(screen.getByTestId("location")).not.toHaveTextContent("atMs");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
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
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    fireEvent.click(screen.getByRole("button", { name: "打开视频证据" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/watch/series-1/video-1?tenantId=local-default&atSeconds=4000"));
    expect(screen.getByTestId("video-player")).toHaveTextContent("video-1 @ 4000");
  });

  it("provides the current episode and exact paused position as generic extra context", async () => {
    renderPage();
    await screen.findByTestId("supervisor-chat");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    expect(screen.getByRole("switch", { name: "发送问题时暂停视频" })).toBeChecked();
    expect(mocks.pauseAndGetSnapshot).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.pauseAndGetSnapshot).toHaveBeenCalledOnce();
    expect(mocks.getSnapshot).not.toHaveBeenCalled();
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-2",
        episodeOrder: 2,
        positionSeconds: 12_000,
      },
    });

    fireEvent.click(screen.getByRole("link", { name: "下一集" }));
    await waitFor(() => expect(screen.getByTestId("video-player")).toHaveTextContent("video-3 @ history"));
    mocks.pauseAndGetSnapshot.mockReturnValue({ positionSeconds: 7_000, wasPlaying: false });
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-3",
        episodeOrder: 3,
        positionSeconds: 7_000,
      },
    });
  });

  it("restores the pause preference and only pauses future sends while it is enabled", async () => {
    const view = renderPage();
    await screen.findByTestId("video-player");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    fireEvent.click(screen.getByRole("switch", { name: "发送问题时暂停视频" }));
    expect(localStorage.getItem("anivag-watch-pause-on-send")).toBe("false");
    view.unmount();

    renderPage();
    const player = await screen.findByTestId("video-player");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    const pauseOnSend = screen.getByRole("switch", { name: "发送问题时暂停视频" });
    const composer = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });
    expect(pauseOnSend).not.toBeChecked();
    mocks.getSnapshot.mockReturnValue({ positionSeconds: 21_567, wasPlaying: true });
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.getSnapshot).toHaveBeenCalledOnce();
    expect(mocks.pauseAndGetSnapshot).not.toHaveBeenCalled();
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: { seriesId: "series-1", videoId: "video-2", episodeOrder: 2, positionSeconds: 21_567 },
    });
    fireEvent.keyDown(composer, { key: "Tab" });
    expect(player).toHaveFocus();
    expect(mocks.resumePlayer).not.toHaveBeenCalled();

    fireEvent.click(pauseOnSend);
    expect(pauseOnSend).toBeChecked();
    expect(localStorage.getItem("anivag-watch-pause-on-send")).toBe("true");
    fireEvent.click(screen.getByRole("tab", { name: "聊天" }));
    mocks.pauseAndGetSnapshot.mockReturnValue({ positionSeconds: 31_234, wasPlaying: true });
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.pauseAndGetSnapshot).toHaveBeenCalledOnce();
    expect(mocks.getSnapshot).toHaveBeenCalledOnce();
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: { seriesId: "series-1", videoId: "video-2", episodeOrder: 2, positionSeconds: 31_234 },
    });
    fireEvent.keyDown(composer, { key: "Tab" });
    expect(mocks.resumePlayer).toHaveBeenCalledOnce();
  });

  it("keeps the latest reported playback context and does not reuse it after switching episodes", async () => {
    renderPage();
    await screen.findByTestId("video-player");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    mocks.pauseAndGetSnapshot.mockReturnValue(null);

    fireEvent.click(screen.getByRole("button", { name: "同步播放位置" }));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-2",
        episodeOrder: 2,
        positionSeconds: 45_000,
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
        positionSeconds: 45_000,
      },
    });
  });

  it("discards the old position when the same video opens at a new timestamp", async () => {
    renderPage("/watch/series-1/video-1");
    await screen.findByTestId("video-player");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    mocks.pauseAndGetSnapshot.mockReturnValue(null);

    fireEvent.click(screen.getByRole("button", { name: "同步播放位置" }));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-1",
        episodeOrder: 1,
        positionSeconds: 45_000,
      },
    });

    fireEvent.click(screen.getByRole("button", { name: "打开视频证据" }));
    await waitFor(() => expect(screen.getByTestId("video-player")).toHaveTextContent("video-1 @ 4000"));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith(null);

    fireEvent.click(screen.getByRole("button", { name: "同步播放位置" }));
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.receiveExtraContext).toHaveBeenLastCalledWith({
      playback: {
        seriesId: "series-1",
        videoId: "video-1",
        episodeOrder: 1,
        positionSeconds: 4_000,
      },
    });
  });

  it("switches focus with Tab without pausing and resumes only playback interrupted by a send", async () => {
    renderPage();
    const player = await screen.findByTestId("video-player");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    const composer = screen.getByRole("textbox", { name: "向 Seekino 助手提问" });
    fireEvent.click(screen.getByRole("tab", { name: "选集" }));
    fireEvent.click(screen.getByRole("button", { name: "收起观看侧栏" }));
    mocks.pauseAndGetSnapshot.mockClear();
    mocks.focusComposer.mockClear();
    mocks.pauseAndGetSnapshot.mockReturnValue({ positionSeconds: 18_000, wasPlaying: true });
    player.focus();
    fireEvent.keyDown(player, { key: "Tab" });

    expect(mocks.pauseAndGetSnapshot).not.toHaveBeenCalled();
    expect(mocks.getSnapshot).not.toHaveBeenCalled();
    expect(screen.getByRole("complementary", { name: "观看侧栏" })).toHaveAttribute("aria-hidden", "false");
    expect(screen.getByRole("tab", { name: "聊天" })).toHaveAttribute("aria-selected", "true");
    expect(composer).toHaveFocus();
    expect(mocks.focusComposer).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    expect(mocks.pauseAndGetSnapshot).toHaveBeenCalledOnce();
    mocks.pauseAndGetSnapshot.mockReturnValue({ positionSeconds: 18_000, wasPlaying: false });
    fireEvent.click(screen.getByRole("button", { name: "捕获播放上下文" }));
    fireEvent.click(screen.getByRole("switch", { name: "发送问题时暂停视频" }));
    fireEvent.keyDown(composer, { key: "Tab", shiftKey: true });
    expect(player).toHaveFocus();
    expect(mocks.focusPlayer).toHaveBeenCalledOnce();
    expect(mocks.resumePlayer).toHaveBeenCalledOnce();

    fireEvent.keyDown(player, { key: "Tab", shiftKey: true });
    expect(composer).toHaveFocus();
    fireEvent.keyDown(composer, { key: "Tab" });
    expect(mocks.resumePlayer).toHaveBeenCalledOnce();
  });

  it("syncs the shell layout and keeps the portaled Chat mounted until leaving the watch route", async () => {
    renderPage("/watch/series-1/video-2", true);
    await screen.findByTestId("video-player");
    const shell = screen.getByTestId("watch-shell");
    const host = document.getElementById("watch-side-panel-host")!;
    const panel = document.getElementById("watch-side-panel")!;
    expect(host).toContainElement(panel);
    expect(screen.getByRole("main")).not.toContainElement(panel);
    expect(shell).toHaveAttribute("data-watch-panel-open", "false");

    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    expect(shell).toHaveAttribute("data-watch-panel-open", "true");
    Object.defineProperty(shell, "getBoundingClientRect", { value: () => ({ left: 0, right: 1200, width: 1200 }) });
    const divider = screen.getByRole("separator", { name: "调整右侧栏宽度" });
    fireEvent.pointerDown(divider, { pointerId: 1, clientX: 700 });
    expect(shell).toHaveAttribute("data-watch-panel-resizing", "true");
    expect(shell).toHaveAttribute("data-watch-panel-width", "500");
    fireEvent.pointerUp(divider, { pointerId: 1 });
    expect(shell).toHaveAttribute("data-watch-panel-resizing", "false");

    fireEvent.click(screen.getByRole("button", { name: "关闭对话面板" }));
    expect(shell).toHaveAttribute("data-watch-panel-open", "false");
    expect(host).toContainElement(panel);
    expect(panel).toHaveAttribute("inert");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    fireEvent.click(screen.getByRole("tab", { name: "选集" }));
    fireEvent.pointerDown(screen.getByRole("separator", { name: "调整右侧栏宽度" }), { pointerId: 2, clientX: 700 });
    fireEvent.click(screen.getByRole("link", { name: "调整集数" }));
    await screen.findByText("资料库页面");
    expect(shell).toHaveAttribute("data-watch-panel-open", "false");
    expect(shell).toHaveAttribute("data-watch-panel-resizing", "false");
    expect(host).toBeEmptyDOMElement();
  });

  it.each([false, true])("includes Chat in the fullscreen target with an app shell: %s", async (withShell) => {
    const originalFullscreen = Object.getOwnPropertyDescriptor(document, "fullscreenElement");
    try {
      renderPage("/watch/series-1/video-2", withShell);
      await screen.findByTestId("video-player");
      fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
      const page = screen.getByRole("main");
      const target = withShell ? screen.getByTestId("watch-shell") : page;
      const requestFullscreen = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(target, "requestFullscreen", { value: requestFullscreen });
      fireEvent.click(screen.getByRole("button", { name: "网页全屏" }));
      expect(requestFullscreen).toHaveBeenCalledOnce();
      expect(target).toContainElement(screen.getByTestId("supervisor-chat"));
      expect(target).toContainElement(screen.getByRole("complementary", { name: "观看侧栏" }));
      Object.defineProperty(document, "fullscreenElement", { configurable: true, value: target });
      fireEvent(document, new Event("fullscreenchange"));
      expect(page).toHaveAttribute("data-fullscreen", "true");
      expect(screen.getByRole("button", { name: "退出网页全屏" })).toBeInTheDocument();
    } finally {
      if (originalFullscreen) Object.defineProperty(document, "fullscreenElement", originalFullscreen);
      else Reflect.deleteProperty(document, "fullscreenElement");
    }
  });

  it("places the Chat toggle in the top bar and brings it into the fullscreen viewer", async () => {
    const headerActions = document.createElement("div");
    headerActions.id = "watch-header-actions";
    document.body.append(headerActions);
    const originalFullscreen = Object.getOwnPropertyDescriptor(document, "fullscreenElement");
    try {
      renderPage();
      await screen.findByTestId("video-player");
      expect(headerActions).toContainElement(screen.getByRole("button", { name: "打开观看侧栏" }));
      const page = screen.getByRole("main");
      Object.defineProperty(document, "fullscreenElement", { configurable: true, value: page });
      fireEvent(document, new Event("fullscreenchange"));
      expect(page).toContainElement(screen.getByRole("button", { name: "打开观看侧栏" }));
      expect(headerActions).toBeEmptyDOMElement();
      fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
      expect(page).toContainElement(screen.getByRole("complementary", { name: "观看侧栏" }));
    } finally {
      if (originalFullscreen) Object.defineProperty(document, "fullscreenElement", originalFullscreen);
      else Reflect.deleteProperty(document, "fullscreenElement");
      headerActions.remove();
    }
  });

  it("resizes the Chat panel with the divider and saves its width", async () => {
    renderPage();
    await screen.findByTestId("video-player");
    fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
    const divider = await screen.findByRole("separator", { name: "调整右侧栏宽度" });
    const page = screen.getByRole("main");
    Object.defineProperty(page, "getBoundingClientRect", { value: () => ({ left: 0, right: 1200, width: 1200 }) });
    fireEvent.keyDown(divider, { key: "ArrowLeft" });
    expect(divider).toHaveAttribute("aria-valuenow", "464");
    fireEvent.pointerDown(divider, { pointerId: 1, clientX: 740 });
    fireEvent.pointerMove(divider, { pointerId: 1, clientX: 600 });
    fireEvent.pointerUp(divider, { pointerId: 1 });
    expect(divider).toHaveAttribute("aria-valuenow", "600");
    expect(page.style.getPropertyValue("--watch-panel-width")).toBe("600px");
    expect(localStorage.getItem("anivag-watch-panel-width")).toBe("600");
  });

  it("clamps a saved wide sidebar to the viewport without losing the preference, and resizes from its visible width", async () => {
    const widthMock = vi.spyOn(window, "innerWidth", "get").mockReturnValue(1440);
    localStorage.setItem("anivag-watch-panel-width", "850");
    try {
      renderPage();
      await screen.findByTestId("video-player");
      fireEvent.click(screen.getByRole("button", { name: "打开观看侧栏" }));
      const divider = screen.getByRole("separator", { name: "调整右侧栏宽度" });
      const page = screen.getByRole("main");
      expect(divider).toHaveAttribute("aria-valuenow", "850");

      widthMock.mockReturnValue(1000);
      fireEvent(window, new Event("resize"));
      expect(divider).toHaveAttribute("aria-valuenow", "580");
      expect(divider).toHaveAttribute("aria-valuemax", "580");
      expect(page.style.getPropertyValue("--watch-panel-width")).toBe("580px");
      expect(localStorage.getItem("anivag-watch-panel-width")).toBe("850");

      widthMock.mockReturnValue(1440);
      fireEvent(window, new Event("resize"));
      expect(divider).toHaveAttribute("aria-valuenow", "850");
      expect(page.style.getPropertyValue("--watch-panel-width")).toBe("850px");

      widthMock.mockReturnValue(1000);
      fireEvent(window, new Event("resize"));
      fireEvent.keyDown(divider, { key: "ArrowRight" });
      expect(divider).toHaveAttribute("aria-valuenow", "556");
      expect(localStorage.getItem("anivag-watch-panel-width")).toBe("556");

      widthMock.mockReturnValue(1440);
      fireEvent(window, new Event("resize"));
      expect(divider).toHaveAttribute("aria-valuenow", "556");
      expect(page.style.getPropertyValue("--watch-panel-width")).toBe("556px");
    } finally {
      widthMock.mockRestore();
    }
  });
});
