import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router";

import { ApiClientError, configureDataSource, PRIMARY_CONNECTION } from "../../api/client";
import type { AuthIdentity, DataSource, SeriesView, VideoSummary } from "../../api/contracts";
import { playbackProgressStorageKey, saveVideoProgress } from "./playbackProgress";
import { SeriesPage } from "./SeriesPage";

const mocks = vi.hoisted(() => ({ getSeries: vi.fn(), updateSeries: vi.fn() }));
vi.mock("../../app/workspace", () => ({ useWorkspace: () => ({ mode: "live", updateSeries: mocks.updateSeries }) }));
vi.mock("../../api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("../../api/client")>(),
  getSeries: mocks.getSeries,
}));
vi.mock("./PlaybackFrame", () => ({
  PlaybackFrame: ({ src, positionSeconds }: { src: string | null; positionSeconds: number }) => (
    <span data-testid="playback-frame" data-src={src} data-position={positionSeconds} />
  ),
}));

function video(videoId: string, episodeOrder: number, uploadSequence = episodeOrder): VideoSummary {
  return {
    videoId, tenantId: "local-default", seriesId: "series-a", episodeOrder, uploadSequence,
    originalFilename: `${videoId}.mp4`, totalBytes: 100, durationSeconds: 120_000,
    uploadStatus: "COMPLETED", parseStatus: "SUCCEEDED", uploadProgress: 100,
    parseProgress: 100, indexStatus: "READY", playbackStatus: "READY",
    playbackUrl: `/api/videos/content?videoId=${videoId}`, playbackUrlExpiresAt: null,
    thumbnail: null, latestJobId: null, lastError: null,
  };
}

const series: SeriesView = {
  seriesId: "series-a", tenantId: "local-default", name: "甲系列", description: "三个朋友，沿着河岸寻找走失的猫。",
  status: "READY", videoCount: 3, readyVideoCount: 3, failedVideoCount: 0,
  pendingUploadCount: 0, cover: null, lastSearch: null, createdAt: 0, updatedAt: 0,
  transcriptLanguageHint: null, importantExtraData: null, characterBible: [], pendingUploads: [],
  videos: [video("a-3", 3), video("a-1", 1), video("a-2", 2)],
};
const localIdentity: AuthIdentity = {
  userId: "local-user", email: null, isGuest: false, tenantId: "local-default",
  publicTenantId: "public-courses", mode: "local", csrfToken: "",
  permissions: { canUpload: true, canManage: true, canConfigure: true },
};
const localSource: DataSource = {
  key: `${PRIMARY_CONNECTION.id}:local`, kind: "local", connection: PRIMARY_CONNECTION,
  tenantId: "local-default", identity: localIdentity,
};

function personalSource(userId: string): DataSource {
  return {
    key: "remote:personal", kind: "personal", connection: { id: "remote", baseUrl: "/remote" },
    tenantId: "personal-tenant", identity: {
      ...localIdentity, userId, email: `${userId}@example.com`, mode: "cloud",
      tenantId: "personal-tenant", csrfToken: `${userId}-csrf`,
    },
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/series/series-a"]}>
        <Routes><Route path="/series/:seriesId" element={<SeriesPage />} /></Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  configureDataSource(localSource);
  mocks.getSeries.mockResolvedValue(series);
});
afterEach(() => { cleanup(); configureDataSource(localSource); vi.clearAllMocks(); });

describe("SeriesPage", () => {
  it("loads the routed series, shows its supplied description, and orders episodes before choosing the first", async () => {
    mocks.getSeries.mockResolvedValue({ ...series, videos: [video("a-3", 3), video("a-1b", 1, 2), video("a-1a", 1, 1)] });
    renderPage();
    expect(await screen.findByRole("heading", { name: "甲系列", level: 1 })).toBeInTheDocument();
    expect(mocks.getSeries).toHaveBeenCalledWith("series-a");
    expect(within(screen.getByRole("region", { name: "系列简介" })).getByText(series.description!)).toBeVisible();
    expect(screen.getByRole("link", { name: "开始观看" })).toHaveAttribute("href", "/watch/series-a/a-1a");
    const episodes = screen.getByRole("region", { name: "系列选集" });
    expect(within(episodes).getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual([
      "/watch/series-a/a-1a", "/watch/series-a/a-1b", "/watch/series-a/a-3",
    ]);
    expect(screen.queryByRole("link", { name: "继续观看" })).not.toBeInTheDocument();
  });

  it("continues the latest playable entry in this series and clamps its position to the current duration", async () => {
    localStorage.setItem(playbackProgressStorageKey(), JSON.stringify([
      { seriesId: "series-b", videoId: "b-1", positionSeconds: 90_000, durationSeconds: 120_000, updatedAt: 60 },
      { seriesId: "series-a", videoId: "removed", positionSeconds: 80_000, durationSeconds: 120_000, updatedAt: 50 },
      { seriesId: "series-a", videoId: "a-3", positionSeconds: 75_000, durationSeconds: 120_000, updatedAt: 40 },
      { seriesId: "series-a", videoId: "a-2", positionSeconds: 45_125, durationSeconds: 120_000, updatedAt: 30 },
      { seriesId: "series-a", videoId: "a-1", positionSeconds: 15_000, durationSeconds: 120_000, updatedAt: 20 },
    ]));
    mocks.getSeries.mockResolvedValue({ ...series, videos: [video("a-1", 1), { ...video("a-2", 2), durationSeconds: 30_000 }, { ...video("a-3", 3), playbackStatus: "PREPARING" }] });
    const stored = localStorage.getItem(playbackProgressStorageKey());
    renderPage();
    expect(await screen.findByRole("link", { name: "继续观看" })).toHaveAttribute("href", "/watch/series-a/a-2");
    const hero = screen.getByRole("region", { name: "系列介绍" });
    expect(within(hero).getByTestId("playback-frame")).toHaveAttribute("data-position", "30000");
    expect(within(hero).getByTestId("playback-frame").getAttribute("data-src")).toContain("videoId=a-2");
    expect(localStorage.getItem(playbackProgressStorageKey())).toBe(stored);
  });

  it("falls back to the first ready video with a URL and leaves unavailable episodes without playback links", async () => {
    saveVideoProgress({ seriesId: "series-a", videoId: "a-1", positionSeconds: 12_000, durationSeconds: 120_000 });
    mocks.getSeries.mockResolvedValue({ ...series, videos: [
      { ...video("a-1", 1), playbackStatus: "PREPARING", playbackUrl: null },
      { ...video("a-2", 2), playbackUrl: null }, video("a-3", 3),
    ] });
    renderPage();
    expect(await screen.findByRole("link", { name: "开始观看" })).toHaveAttribute("href", "/watch/series-a/a-3");
    const episodes = screen.getByRole("region", { name: "系列选集" });
    expect(within(episodes).getAllByRole("listitem")).toHaveLength(3);
    expect(within(episodes).getAllByRole("link")).toHaveLength(1);
    expect(within(episodes).getByRole("link")).toHaveAttribute("href", "/watch/series-a/a-3");
    expect(within(episodes).getByText("a-1.mp4")).toBeVisible();
    expect(within(episodes).getByText("a-2.mp4")).toBeVisible();
    expect(screen.queryByRole("link", { name: "继续观看" })).not.toBeInTheDocument();
  });

  it("keeps continue watching isolated between accounts with identical series and video IDs", async () => {
    configureDataSource(personalSource("user-a"));
    saveVideoProgress({ seriesId: "series-a", videoId: "a-2", positionSeconds: 45_125, durationSeconds: 120_000 });
    const firstKey = playbackProgressStorageKey();
    const firstHistory = localStorage.getItem(firstKey);
    configureDataSource(personalSource("user-b"));
    const otherAccount = renderPage();
    expect(await screen.findByRole("link", { name: "开始观看" })).toHaveAttribute("href", "/watch/series-a/a-1");
    expect(screen.queryByRole("link", { name: "继续观看" })).not.toBeInTheDocument();
    expect(localStorage.getItem(playbackProgressStorageKey())).toBeNull();
    otherAccount.unmount();
    configureDataSource(personalSource("user-a"));
    renderPage();
    expect(await screen.findByRole("link", { name: "继续观看" })).toHaveAttribute("href", "/watch/series-a/a-2");
    expect(within(screen.getByRole("region", { name: "系列介绍" })).getByTestId("playback-frame")).toHaveAttribute("data-position", "45125");
    expect(localStorage.getItem(firstKey)).toBe(firstHistory);
  });

  it("does not invent a description when none was supplied", async () => {
    mocks.getSeries.mockResolvedValue({ ...series, description: null });
    renderPage();
    await screen.findByRole("heading", { name: "甲系列", level: 1 });
    expect(screen.getByText("还没有填写系列简介。")).toBeVisible();
    expect(screen.queryByText(/故事还在继续|一起寻找故事里的名场面/)).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "系列选集" })).toBeVisible();
  });

  it("expands supplied Markdown and asset images without claiming the series has no introduction", async () => {
    mocks.getSeries.mockResolvedValue({ ...series, description: null, transcriptLanguageHint: "ja",
      importantExtraData: { format: "markdown", content: "## 高松灯\n\n![灯的立绘](asset://simg_tomori)\n\n乐队的**主唱**。" },
    });
    const user = userEvent.setup();
    const originalBounds = HTMLElement.prototype.getBoundingClientRect;
    const boundsMock = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const bounds = originalBounds.call(this);
      return this.getAttribute("aria-label") === "系列背景资料" ? { ...bounds, height: 500, bottom: bounds.top + 500 } : bounds;
    });
    try {
      renderPage();
      const expand = await screen.findByRole("button", { name: "展开背景介绍" });
      expect(expand).toHaveAttribute("aria-expanded", "false");
      expect(expand).toHaveAttribute("aria-controls", "series-background-content");
      expect(screen.queryByText("还没有填写系列简介。")).not.toBeInTheDocument();
      await user.click(expand);
      expect(screen.getByRole("heading", { name: "高松灯", level: 2 })).toBeVisible();
      expect(screen.getByText("主唱").tagName).toBe("STRONG");
      expect(screen.getByRole("img", { name: "灯的立绘" })).toHaveAttribute("src", "/api/media/images/content?tenantId=local-default&imageId=simg_tomori");
      expect(document.getElementById("series-background-content")).toContainElement(screen.getByRole("heading", { name: "高松灯" }));
      expect(screen.queryByRole("button", { name: "编辑背景" })).not.toBeInTheDocument();
      expect(screen.queryByText(/ASR 原文语言/)).not.toBeInTheDocument();
      const collapse = screen.getByRole("button", { name: "收起背景介绍" });
      expect(collapse).toHaveAttribute("aria-expanded", "true");
      await user.click(collapse);
      expect(screen.getByRole("button", { name: "展开背景介绍" })).toHaveAttribute("aria-expanded", "false");
      expect(mocks.updateSeries).not.toHaveBeenCalled();
    } finally {
      boundsMock.mockRestore();
    }
  });

  it("shows both the existing description and background without duplicating the description", async () => {
    mocks.getSeries.mockResolvedValue({ ...series, importantExtraData: { format: "markdown", content: "## 故事背景\n\n猫曾经住在河岸边。" } });
    renderPage();
    await screen.findByRole("heading", { name: "故事背景" });
    expect(screen.getAllByText(series.description!)).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "故事背景" })).toBeVisible();
    expect(screen.getByText("猫曾经住在河岸边。")).toBeVisible();
    expect(screen.queryByRole("button", { name: "展开背景介绍" })).not.toBeInTheDocument();
    expect(screen.queryByText("还没有填写系列简介。")).not.toBeInTheDocument();
    expect(mocks.updateSeries).not.toHaveBeenCalled();
  });

  it.each([
    { format: "legacy Markdown", data: { seriesBible: "## 原始人物背景\n\n来自用户的原始资料。" }, text: "来自用户的原始资料。" },
    { format: "JSON", data: { format: "json", content: ' \n{ "人物": "## 原始人物背景", "备注": ["原样保留"] }\n ' }, text: ' \n{ "人物": "## 原始人物背景", "备注": ["原样保留"] }\n ' },
  ])("preserves the original $format background in the read-only viewer", async ({ format, data, text }) => {
    const original = JSON.stringify(data);
    mocks.getSeries.mockResolvedValue({ ...series, description: null, importantExtraData: data });
    renderPage();
    await screen.findByRole("region", { name: "系列背景资料" });
    if (format === "JSON") {
      expect(screen.getByRole("region", { name: "系列背景资料" }).querySelector("pre")?.textContent).toBe(text);
      expect(screen.queryByRole("heading", { name: "原始人物背景" })).not.toBeInTheDocument();
    } else {
      expect(screen.getByRole("heading", { name: "原始人物背景", level: 2 })).toBeVisible();
      expect(screen.getByText(text)).toBeVisible();
    }
    expect(JSON.stringify(data)).toBe(original);
    expect(screen.queryByRole("button", { name: "展开背景介绍" })).not.toBeInTheDocument();
    expect(screen.queryByText("还没有填写系列简介。")).not.toBeInTheDocument();
    expect(mocks.updateSeries).not.toHaveBeenCalled();
  });

  it("shows the missing-introduction message only when both description and background are blank", async () => {
    mocks.getSeries.mockResolvedValue({ ...series, description: " \n", importantExtraData: { format: "markdown", content: " \n\t" } });
    renderPage();
    expect(await screen.findByText("还没有填写系列简介。")).toBeVisible();
    expect(screen.queryByRole("button", { name: "展开背景介绍" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "系列背景资料" })).not.toBeInTheDocument();
    expect(mocks.updateSeries).not.toHaveBeenCalled();
  });

  it("shows an empty-series state without a playback action", async () => {
    mocks.getSeries.mockResolvedValue({ ...series, videoCount: 0, readyVideoCount: 0, videos: [] });
    renderPage();
    await screen.findByRole("heading", { name: "甲系列", level: 1 });
    expect(within(screen.getByRole("region", { name: "系列选集" })).getByText("这个系列还没有视频。")).toBeVisible();
    expect(screen.queryByRole("link", { name: "开始观看" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "继续观看" })).not.toBeInTheDocument();
  });

  it("shows a loading status while the series request is pending", () => {
    mocks.getSeries.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(screen.getByRole("status")).toHaveTextContent(/正在读取/);
    expect(screen.queryByRole("link", { name: "开始观看" })).not.toBeInTheDocument();
  });

  it("reports the API error without exposing a playback action", async () => {
    mocks.getSeries.mockRejectedValue(new ApiClientError(404, { message: "系列已移除" }));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("系列已移除");
    expect(screen.queryByRole("link", { name: "开始观看" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "继续观看" })).not.toBeInTheDocument();
  });
});
