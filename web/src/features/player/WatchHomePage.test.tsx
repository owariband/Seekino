import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import type { SeriesSummary, SeriesView, VideoSummary } from "../../api/contracts";
import { WatchHomePage } from "./WatchHomePage";

const mocks = vi.hoisted(() => ({
  getSeries: vi.fn(),
  readVideoProgress: vi.fn(),
  readWatchHistory: vi.fn(),
  saveVideoProgress: vi.fn(),
  useWorkspace: vi.fn(),
  useAuth: vi.fn(),
}));

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  getSeries: mocks.getSeries,
}));
vi.mock("../../app/workspace", () => ({ useWorkspace: mocks.useWorkspace }));
vi.mock("../auth/AuthProvider", () => ({ useAuth: mocks.useAuth }));
vi.mock("./playbackProgress", () => ({
  readVideoProgress: mocks.readVideoProgress,
  readWatchHistory: mocks.readWatchHistory,
  saveVideoProgress: mocks.saveVideoProgress,
}));
vi.mock("./PlaybackFrame", () => ({
  PlaybackFrame: ({ src, positionSeconds, poster }: { src: string | null; positionSeconds: number; poster?: string | null }) => (
    <div data-testid="playback-frame" data-src={src} data-position={positionSeconds} data-poster={poster} />
  ),
}));

const firstSeries: SeriesSummary = {
  visibility: "private", access: "member", publisher: null,
  seriesId: "series-a", tenantId: "local-default", name: "甲系列", description: null,
  status: "READY", videoCount: 3, readyVideoCount: 3, failedVideoCount: 0,
  pendingUploadCount: 0, cover: null, lastSearch: null, createdAt: 0, updatedAt: 0,
};
const secondSeries: SeriesSummary = {
  ...firstSeries, seriesId: "series-b", name: "乙系列", videoCount: 2, readyVideoCount: 2,
};

function video(seriesId: string, videoId: string, episodeOrder: number): VideoSummary {
  return {
    videoId, tenantId: "local-default", seriesId, episodeOrder, uploadSequence: episodeOrder,
    originalFilename: `${videoId}.mp4`, totalBytes: 100, durationSeconds: 120_000,
    uploadStatus: "COMPLETED", parseStatus: "SUCCEEDED", uploadProgress: 100,
    parseProgress: 100, indexStatus: "READY", playbackStatus: "READY",
    playbackUrl: `/api/videos/content?videoId=${videoId}`, playbackUrlExpiresAt: null,
    thumbnail: null, latestJobId: null, lastError: null,
  };
}

function detail(summary: SeriesSummary, videos: VideoSummary[]): SeriesView {
  return { ...summary, transcriptLanguageHint: null, importantExtraData: null,
    characterBible: [], videos, pendingUploads: [] };
}

function CurrentLocation() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/watch"]}>
        <Routes>
          <Route path="/watch" element={<><WatchHomePage /><CurrentLocation /></>} />
          <Route path="/watch/:seriesId/:videoId" element={<CurrentLocation />} />
          <Route path="/series/:seriesId" element={<CurrentLocation />} />
          <Route path="/library" element={<p>资料库页面</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.useAuth.mockReturnValue({ activeSource: { kind: "local" }, canUpload: true });
  mocks.useWorkspace.mockReturnValue({ series: [firstSeries, secondSeries], isLoading: false, error: null });
  mocks.getSeries.mockImplementation(async (seriesId: string) => seriesId === firstSeries.seriesId
    ? detail(firstSeries, [video("series-a", "a-3", 3), video("series-a", "a-1", 1), video("series-a", "a-2", 2)])
    : detail(secondSeries, [video("series-b", "b-2", 2), video("series-b", "b-1", 1)]));
  mocks.readWatchHistory.mockReturnValue([]);
  mocks.readVideoProgress.mockReturnValue(null);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("WatchHomePage", () => {
  it("preserves a public Series tenant and separates owner controls from the card link", async () => {
    const shared = { ...secondSeries, tenantId: "another-owner", access: "public" as const,
      visibility: "public" as const, publisher: { kind: "user" as const, displayName: "分享者", avatar: null } };
    mocks.useWorkspace.mockReturnValue({ series: [firstSeries, shared], isLoading: false, error: null });
    mocks.getSeries.mockImplementation(async (id: string) => detail(id === shared.seriesId ? shared : firstSeries, []));
    renderPage();
    const card = screen.getByRole("link", { name: /乙系列/ });
    expect(card).toHaveAttribute("href", "/series/series-b?tenantId=another-owner");
    expect(screen.getByText("分享者 分享")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "乙系列 的系列选项" })).not.toBeInTheDocument();
    const ownerMenu = screen.getByRole("button", { name: "甲系列 的系列选项" });
    expect(ownerMenu.closest("a")).toBeNull();
    await waitFor(() => expect(mocks.getSeries).toHaveBeenCalledWith("series-b", "another-owner"));
  });

  it("shows the most recently watched video and its saved frame in the continue hero", async () => {
    mocks.readWatchHistory.mockReturnValue([{
      seriesId: "series-b", videoId: "b-2", positionSeconds: 45_000,
      durationSeconds: 120_000, updatedAt: 10,
    }]);
    renderPage();

    const hero = await screen.findByRole("region", { name: "继续观看推荐" });
    const continueLink = await within(hero).findByRole("link", { name: "继续观看" });
    expect(continueLink).toHaveAttribute("href", "/watch/series-b/b-2?tenantId=local-default");
    expect(within(hero).getByRole("heading", { name: "乙系列", level: 2 })).toBeInTheDocument();
    expect(within(hero).getByText("第 2 集")).toBeInTheDocument();
    expect(within(hero).getByText("上次看到 00:45")).toBeInTheDocument();
    const frame = within(hero).getByTestId("playback-frame");
    expect(frame.getAttribute("data-src")).toContain("videoId=b-2");
    expect(frame).toHaveAttribute("data-position", "45000");
    expect(screen.queryByRole("region", { name: "系列选集" })).not.toBeInTheDocument();
    expect(within(hero).getByRole("link", { name: "查看系列" })).toHaveAttribute("href", "/series/series-b?tenantId=local-default");
    expect(mocks.saveVideoProgress).not.toHaveBeenCalled();
  });

  it("continues the latest watched video directly without opening series details", async () => {
    mocks.readWatchHistory.mockReturnValue([{
      seriesId: "series-b", videoId: "b-2", positionSeconds: 45_000,
      durationSeconds: 120_000, updatedAt: 10,
    }]);
    renderPage();
    const user = userEvent.setup();
    const hero = await screen.findByRole("region", { name: "继续观看推荐" });
    await within(hero).findByRole("link", { name: "继续观看" });

    await user.click(within(hero).getByRole("link", { name: "继续观看" }));
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/watch\/series-b\/b-2\?tenantId=local-default$/);
  });

  it("offers the first playable episode in the hero when there is no watch history", async () => {
    mocks.getSeries.mockResolvedValue(detail(firstSeries, [
      { ...video("series-a", "a-1", 1), playbackStatus: "PREPARING", playbackUrl: null },
      video("series-a", "a-3", 3), video("series-a", "a-2", 2),
    ]));
    renderPage();

    const hero = await screen.findByRole("region", { name: "继续观看推荐" });
    expect(await within(hero).findByRole("link", { name: "播放本集" })).toHaveAttribute("href", "/watch/series-a/a-2?tenantId=local-default");
    expect(within(hero).getByRole("heading", { name: "甲系列", level: 2 })).toBeInTheDocument();
    expect(within(hero).getByText("第 2 集")).toBeInTheDocument();
    expect(within(hero).queryByRole("link", { name: "继续观看" })).not.toBeInTheDocument();
    expect(within(hero).queryByText(/上次看到/)).not.toBeInTheDocument();
    expect(within(hero).getByTestId("playback-frame").getAttribute("data-src")).toContain("videoId=a-2");
    expect(within(hero).getByTestId("playback-frame")).toHaveAttribute("data-position", "12000");
    expect(mocks.saveVideoProgress).not.toHaveBeenCalled();
  });

  it.each(["removed", "unavailable"])("falls back to a playable episode when the latest watched video is %s", async (state) => {
    mocks.readWatchHistory.mockReturnValue([{
      seriesId: "series-b", videoId: "b-2", positionSeconds: 45_000,
      durationSeconds: 120_000, updatedAt: 10,
    }]);
    mocks.getSeries.mockResolvedValue(detail(secondSeries, [
      video("series-b", "b-1", 1),
      ...(state === "unavailable" ? [{ ...video("series-b", "b-2", 2), playbackStatus: "PREPARING" as const }] : []),
    ]));
    renderPage();

    const hero = await screen.findByRole("region", { name: "继续观看推荐" });
    expect(await within(hero).findByRole("link", { name: "播放本集" })).toHaveAttribute("href", "/watch/series-b/b-1?tenantId=local-default");
    expect(within(hero).queryByRole("link", { name: "继续观看" })).not.toBeInTheDocument();
    expect(within(hero).queryByText(/上次看到/)).not.toBeInTheDocument();
    expect(within(hero).getByTestId("playback-frame").getAttribute("data-src")).toContain("videoId=b-1");
    expect(within(hero).getByTestId("playback-frame")).toHaveAttribute("data-position", "12000");
    expect(mocks.saveVideoProgress).not.toHaveBeenCalled();
  });

  it("opens a dedicated series page from a homepage series card", async () => {
    renderPage();
    const user = userEvent.setup();
    expect(screen.queryByRole("region", { name: "系列选集" })).not.toBeInTheDocument();
    const seriesCard = screen.getByRole("link", { name: /乙系列/ });
    expect(seriesCard).toHaveAttribute("href", "/series/series-b?tenantId=local-default");
    await user.click(seriesCard);
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/series\/series-b\?tenantId=local-default$/);
  });

  it("uses the last watched frame over the series cover, keeping that cover as a loading fallback", async () => {
    const coveredSeries = { ...secondSeries, cover: { imageId: "cover-b", url: "/series-b-cover.jpg", expiresAt: null } };
    mocks.useWorkspace.mockReturnValue({ series: [firstSeries, coveredSeries], isLoading: false, error: null });
    mocks.readWatchHistory.mockReturnValue([{
      seriesId: "series-b", videoId: "b-2", positionSeconds: 45_000,
      durationSeconds: 120_000, updatedAt: 10,
    }]);
    renderPage();

    const hero = screen.getByRole("region", { name: "继续观看推荐" });
    await within(hero).findByRole("link", { name: "继续观看" });
    const seriesCard = screen.getByRole("link", { name: /乙系列/ });
    const frame = await within(seriesCard).findByTestId("playback-frame");
    await waitFor(() => expect(frame).toHaveAttribute("data-position", "45000"));
    expect(frame).toHaveAttribute("data-src", expect.stringContaining("videoId=b-2"));
    expect(frame).toHaveAttribute("data-poster", expect.stringContaining("/series-b-cover.jpg"));
    expect(within(hero).getByTestId("playback-frame")).toHaveAttribute("data-position", "45000");
  });

  it("updates a series card to its latest saved progress when returning to the homepage", async () => {
    mocks.useWorkspace.mockReturnValue({ series: [secondSeries], isLoading: false, error: null });
    mocks.getSeries.mockResolvedValue(detail(secondSeries, [
      video("series-b", "b-2", 2),
      { ...video("series-b", "b-1", 1), durationSeconds: 1_200_000 },
    ]));
    mocks.readWatchHistory.mockReturnValue([{
      seriesId: "series-b", videoId: "b-2", positionSeconds: 45_000,
      durationSeconds: 120_000, updatedAt: 10,
    }]);
    const { unmount } = renderPage();
    const seriesCard = screen.getByRole("link", { name: /乙系列/ });
    const representativeFrame = await within(seriesCard).findByTestId("playback-frame");
    expect(representativeFrame.getAttribute("data-src")).toContain("videoId=b-2");
    expect(representativeFrame).toHaveAttribute("data-position", "45000");

    mocks.readWatchHistory.mockReturnValue([{
      seriesId: "series-b", videoId: "b-2", positionSeconds: 90_000,
      durationSeconds: 120_000, updatedAt: 20,
    }]);
    unmount();
    renderPage();
    const updatedCard = screen.getByRole("link", { name: /乙系列/ });
    const updatedFrame = await within(updatedCard).findByTestId("playback-frame");
    expect(within(screen.getByRole("region", { name: "继续观看推荐" })).getByTestId("playback-frame"))
      .toHaveAttribute("data-position", "90000");
    expect(updatedFrame.getAttribute("data-src")).toContain("videoId=b-2");
    expect(updatedFrame).toHaveAttribute("data-position", "90000");
    expect(mocks.saveVideoProgress).not.toHaveBeenCalled();
  });

  it.each([false, true])("keeps the selected Summary artwork when history is absent or stale: stale=%s", async (stale) => {
    const coveredSeries = { ...firstSeries, cover: { imageId: "summary-cover", url: "/chosen-summary.jpg", expiresAt: null } };
    mocks.useWorkspace.mockReturnValue({ series: [coveredSeries], isLoading: false, error: null });
    mocks.getSeries.mockResolvedValue(detail(coveredSeries, [video("series-a", "a-1", 1), video("series-a", "a-2", 2)]));
    mocks.readWatchHistory.mockReturnValue(stale ? [{ seriesId: "series-a", videoId: "deleted", positionSeconds: 45_000,
      durationSeconds: 120_000, updatedAt: 10 }] : []);
    renderPage();
    const hero = screen.getByRole("region", { name: "继续观看推荐" });
    expect(await within(hero).findByRole("link", { name: "播放本集" })).toHaveAttribute("href", "/watch/series-a/a-1?tenantId=local-default");
    for (const region of [hero, screen.getByRole("link", { name: /甲系列.*查看系列/ })]) {
      const frame = within(region).getByTestId("playback-frame");
      expect(frame).toHaveAttribute("data-poster", expect.stringContaining("/chosen-summary.jpg"));
      expect(frame).not.toHaveAttribute("data-src");
    }
    expect(mocks.saveVideoProgress).not.toHaveBeenCalled();
  });

  it("shows an upload path when the library has no video series", () => {
    mocks.useWorkspace.mockReturnValue({ series: [], isLoading: false, error: null });
    renderPage();

    expect(screen.getByText("资料库里还没有视频。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前往资料库上传视频" })).toHaveAttribute("href", "/library");
    expect(screen.queryByRole("region", { name: "系列选集" })).not.toBeInTheDocument();
  });

  it("shows a read-only empty state for public data", () => {
    mocks.useWorkspace.mockReturnValue({ series: [], isLoading: false, error: null });
    mocks.useAuth.mockReturnValue({ activeSource: { kind: "public" }, canUpload: false });
    renderPage();

    expect(screen.getByText("公开资料里还没有视频。")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "前往资料库上传视频" })).not.toBeInTheDocument();
  });

  it("keeps the local upload path when a remote account is signed in", () => {
    mocks.useWorkspace.mockReturnValue({ series: [], isLoading: false, error: null });
    mocks.useAuth.mockReturnValue({
      activeSource: { kind: "local" }, canUpload: true,
      accountIdentity: { isGuest: false, email: "learner@example.com" },
    });
    renderPage();

    expect(screen.getByText("资料库里还没有视频。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前往资料库上传视频" })).toHaveAttribute("href", "/library");
    expect(screen.queryByText("公开资料里还没有视频。")).not.toBeInTheDocument();
  });

  it("keeps the series details link available when an existing series has no episodes", async () => {
    mocks.useWorkspace.mockReturnValue({ series: [firstSeries], isLoading: false, error: null });
    mocks.getSeries.mockResolvedValue(detail(firstSeries, []));
    renderPage();
    const user = userEvent.setup();
    expect(screen.queryByRole("link", { name: /播放本集|继续观看/ })).not.toBeInTheDocument();
    const seriesLink = within(screen.getByRole("region", { name: "继续观看推荐" })).getByRole("link", { name: "查看系列" });
    expect(seriesLink).toHaveAttribute("href", "/series/series-a?tenantId=local-default");
    await user.click(seriesLink);
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/series\/series-a\?tenantId=local-default$/);
  });
});
