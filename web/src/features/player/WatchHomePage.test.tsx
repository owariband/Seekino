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
  useWorkspace: vi.fn(),
}));

vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  getSeries: mocks.getSeries,
}));
vi.mock("../../app/workspace", () => ({ useWorkspace: mocks.useWorkspace }));
vi.mock("./playbackProgress", () => ({
  readVideoProgress: mocks.readVideoProgress,
  readWatchHistory: mocks.readWatchHistory,
}));

const firstSeries: SeriesSummary = {
  seriesId: "series-a", workspaceId: "local-default", name: "甲系列", description: null,
  status: "READY", videoCount: 3, readyVideoCount: 3, failedVideoCount: 0,
  pendingUploadCount: 0, cover: null, lastSearch: null, createdAt: 0, updatedAt: 0,
};
const secondSeries: SeriesSummary = {
  ...firstSeries, seriesId: "series-b", name: "乙系列", videoCount: 2, readyVideoCount: 2,
};

function video(seriesId: string, videoId: string, episodeOrder: number): VideoSummary {
  return {
    videoId, workspaceId: "local-default", seriesId, episodeOrder, uploadSequence: episodeOrder,
    originalFilename: `${videoId}.mp4`, totalBytes: 100, durationMs: 120_000,
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
          <Route path="/library" element={<p>资料库页面</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.useWorkspace.mockReturnValue({ series: [firstSeries, secondSeries], isLoading: false, error: null });
  mocks.getSeries.mockImplementation(async (seriesId: string) => seriesId === firstSeries.seriesId
    ? detail(firstSeries, [video("series-a", "a-3", 3), video("series-a", "a-1", 1), video("series-a", "a-2", 2)])
    : detail(secondSeries, [video("series-b", "b-2", 2), video("series-b", "b-1", 1)]));
  mocks.readWatchHistory.mockReturnValue([]);
  mocks.readVideoProgress.mockReturnValue(null);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("WatchHomePage", () => {
  it("offers the most recently watched video as the continue entry", async () => {
    mocks.readWatchHistory.mockReturnValue([{
      seriesId: "series-b", videoId: "b-2", positionMs: 45_000,
      durationMs: 120_000, updatedAt: 10,
    }]);
    renderPage();

    const continueLink = await screen.findByRole("link", { name: /继续观看/ });
    expect(continueLink).toHaveAttribute("href", "/watch/series-b/b-2");
    await waitFor(() => expect(continueLink).toHaveTextContent("乙系列 · EP 02"));
    expect(continueLink).toHaveTextContent("上次看到 00:45");
    expect(await screen.findByRole("heading", { name: "乙系列" })).toBeInTheDocument();
  });

  it("switches series and lists episodes in episode order", async () => {
    renderPage();
    const user = userEvent.setup();
    const episodes = await screen.findByRole("region", { name: "系列选集" });
    expect((await within(episodes).findAllByRole("link", { name: /EP / })).map((item) => item.textContent?.slice(0, 5)))
      .toEqual(["EP 01", "EP 02", "EP 03"]);
    expect(within(episodes).getByRole("link", { name: "开始观看" })).toHaveAttribute("href", "/watch/series-a/a-1");

    await user.click(screen.getByRole("button", { name: /乙系列/ }));
    await waitFor(() => expect(within(episodes).getAllByRole("link", { name: /EP / }).map((item) => item.textContent?.slice(0, 5)))
      .toEqual(["EP 01", "EP 02"]));
    await user.click(within(episodes).getByRole("link", { name: /EP 02/ }));
    expect(screen.getByTestId("location")).toHaveTextContent("/watch/series-b/b-2");
  });

  it("shows an upload path when the library has no video series", () => {
    mocks.useWorkspace.mockReturnValue({ series: [], isLoading: false, error: null });
    renderPage();

    expect(screen.getByText("资料库里还没有视频。")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "前往资料库上传视频" })).toHaveAttribute("href", "/library");
    expect(screen.queryByRole("region", { name: "系列选集" })).not.toBeInTheDocument();
  });

  it("shows an empty episode state for an existing series", async () => {
    mocks.useWorkspace.mockReturnValue({ series: [firstSeries], isLoading: false, error: null });
    mocks.getSeries.mockResolvedValue(detail(firstSeries, []));
    renderPage();

    expect(await screen.findByText("这个系列还没有视频。")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "开始观看" })).not.toBeInTheDocument();
  });
});
