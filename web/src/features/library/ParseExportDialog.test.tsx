import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError } from "../../api/client";
import type { SeriesView, VideoSummary } from "../../api/contracts";
import { ParseExportDialog } from "./ParseExportDialog";

const mocks = vi.hoisted(() => ({ getSeries: vi.fn(), getParseExportOptions: vi.fn(), exportParsedVideos: vi.fn() }));
vi.mock("../../api/client", async (original) => ({ ...await original<typeof import("../../api/client")>(), ...mocks }));

const video: VideoSummary = {
  videoId: "video-a", tenantId: "owner-tenant", seriesId: "series-a", episodeOrder: 1, uploadSequence: 0,
  originalFilename: "第一集.mp4", totalBytes: 1, durationSeconds: 120000, uploadStatus: "COMPLETED",
  parseStatus: "SUCCEEDED", uploadProgress: 100, parseProgress: 100, indexStatus: "READY",
  playbackStatus: "READY", playbackUrl: "/video-a", playbackUrlExpiresAt: null,
  thumbnail: null, latestJobId: "task-a", lastError: null,
};
const series: SeriesView = {
  seriesId: "series-a", tenantId: "owner-tenant", name: "我的系列", description: null,
  visibility: "private", access: "member", publisher: null, status: "READY", videoCount: 2,
  readyVideoCount: 1, failedVideoCount: 1, pendingUploadCount: 0, cover: null, lastSearch: null,
  createdAt: 0, updatedAt: 0, transcriptLanguageHint: null, importantExtraData: null, characterBible: [], pendingUploads: [],
  videos: [video, { ...video, videoId: "video-b", episodeOrder: 2, originalFilename: "第二集.mp4", indexStatus: "FAILED", playbackStatus: "MISSING", playbackUrl: null }],
};
const result = { blob: new Blob(['{"text":"对白"}\n']), filename: "parse-export.jsonl", summary: { record_count: 2, event_count: 7, skipped: { no_matching_events: 1 } } };
const showModalDescriptor = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "showModal");
const closeDescriptor = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "close");

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onClose = vi.fn();
  const view = render(<QueryClientProvider client={client}><ParseExportDialog series={series} onClose={onClose} /></QueryClientProvider>);
  return { ...view, onClose, user: userEvent.setup() };
}

beforeEach(() => {
  mocks.getSeries.mockReset().mockResolvedValue(series);
  mocks.getParseExportOptions.mockReset().mockResolvedValue({ eventTypes: ["DIALOGUE", "ACTION", "FUTURE_EVENT"] });
  mocks.exportParsedVideos.mockReset().mockResolvedValue(result);
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function (this: HTMLDialogElement) { this.setAttribute("open", ""); } });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function (this: HTMLDialogElement) { this.removeAttribute("open"); } });
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = vi.fn(() => "blob:parse-export");
    static revokeObjectURL = vi.fn();
  });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  if (showModalDescriptor) Object.defineProperty(HTMLDialogElement.prototype, "showModal", showModalDescriptor);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  if (closeDescriptor) Object.defineProperty(HTMLDialogElement.prototype, "close", closeDescriptor);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ParseExportDialog", () => {
  it("selects every series video regardless of playback or index state and uses server-provided types", async () => {
    const { user } = renderDialog();
    expect(screen.getByRole("dialog", { name: "导出解析数据" })).toBeInTheDocument();
    expect(await screen.findByRole("checkbox", { name: /第二集.mp4/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "FUTURE_EVENT" })).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "场景" })).not.toBeInTheDocument();
    expect(mocks.getSeries).toHaveBeenCalledWith("series-a", "owner-tenant");
    expect(mocks.getParseExportOptions).toHaveBeenCalledWith("owner-tenant", expect.any(AbortSignal));
    await user.click(screen.getByRole("button", { name: "下载 JSONL" }));
    expect(mocks.exportParsedVideos).toHaveBeenCalledWith({ tenantId: "owner-tenant", videoIds: ["video-a", "video-b"], eventTypes: ["DIALOGUE", "ACTION", "FUTURE_EVENT"] }, expect.any(AbortSignal));
    expect(URL.createObjectURL).toHaveBeenCalledWith(result.blob);
    const anchor = vi.mocked(HTMLAnchorElement.prototype.click).mock.instances[0] as HTMLAnchorElement;
    expect(anchor.download).toBe("parse-export.jsonl");
    expect(anchor.href).toBe("blob:parse-export");
    expect(anchor.isConnected).toBe(false);
    expect(await screen.findByRole("status")).toHaveTextContent("2 条记录，7 个事件");
    expect(screen.getByRole("status")).toHaveTextContent("跳过 1 个无匹配事件");
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:parse-export"));
  });

  it("supports dialogue-only, multiple types and empty-selection guards", async () => {
    const { user } = renderDialog();
    await screen.findByRole("checkbox", { name: "对白" });
    await user.click(screen.getByRole("button", { name: "仅对白" }));
    expect(screen.getByRole("checkbox", { name: "动作" })).not.toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: /第二集.mp4/ }));
    await user.click(screen.getByRole("button", { name: "下载 JSONL" }));
    expect(mocks.exportParsedVideos).toHaveBeenLastCalledWith({ tenantId: "owner-tenant", videoIds: ["video-a"], eventTypes: ["DIALOGUE"] }, expect.any(AbortSignal));
    await user.click(screen.getByRole("checkbox", { name: "动作" }));
    await user.click(screen.getByRole("button", { name: "下载 JSONL" }));
    expect(mocks.exportParsedVideos).toHaveBeenLastCalledWith({ tenantId: "owner-tenant", videoIds: ["video-a"], eventTypes: ["DIALOGUE", "ACTION"] }, expect.any(AbortSignal));
    await user.click(screen.getByRole("button", { name: "清空视频" }));
    expect(screen.getByRole("button", { name: "下载 JSONL" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "全选视频" }));
    await user.click(screen.getByRole("button", { name: "仅对白" }));
    await user.click(screen.getByRole("checkbox", { name: "对白" }));
    expect(screen.getByRole("button", { name: "下载 JSONL" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "全选类型" }));
    expect(screen.getByRole("button", { name: "下载 JSONL" })).toBeEnabled();
  });

  it("reports an empty result without downloading a file", async () => {
    mocks.exportParsedVideos.mockResolvedValue({ ...result, blob: new Blob(), summary: { record_count: 0, event_count: 0, skipped: { no_matching_events: 4 } } });
    const { user } = renderDialog();
    await screen.findByRole("checkbox", { name: "对白" });
    await user.click(screen.getByRole("button", { name: "下载 JSONL" }));
    expect(await screen.findByRole("status")).toHaveTextContent("没有符合所选类型的事件");
    expect(screen.getByRole("status")).toHaveTextContent("跳过 4 个");
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("keeps source errors visible and permits retry without losing selections", async () => {
    mocks.exportParsedVideos.mockRejectedValueOnce(new ApiClientError(409, { message: "正式解析内容尚未完成" }));
    const { user } = renderDialog();
    await screen.findByRole("checkbox", { name: "对白" });
    await user.click(screen.getByRole("button", { name: "仅对白" }));
    await user.click(screen.getByRole("button", { name: "下载 JSONL" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("正式解析内容尚未完成");
    expect(screen.getByRole("checkbox", { name: "对白" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "动作" })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "下载 JSONL" }));
    expect(await screen.findByRole("status")).toHaveTextContent("已开始下载");
  });

  it("disables export when loading options fails and can reload them", async () => {
    mocks.getParseExportOptions.mockRejectedValueOnce(new ApiClientError(403, { message: "无权访问" }));
    const { user } = renderDialog();
    expect(await screen.findByRole("alert")).toHaveTextContent("无权访问");
    expect(screen.getByRole("button", { name: "下载 JSONL" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByRole("checkbox", { name: "对白" });
    expect(screen.getByRole("button", { name: "下载 JSONL" })).toBeEnabled();
  });

  it("cancels pending downloads on unmount and ignores a late response", async () => {
    let release!: (value: typeof result) => void;
    mocks.exportParsedVideos.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    const { user, unmount, onClose } = renderDialog();
    await screen.findByRole("checkbox", { name: "对白" });
    await user.click(screen.getByRole("button", { name: "下载 JSONL" }));
    expect(screen.getByRole("button", { name: "正在导出…" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "关闭导出" }));
    expect(onClose).toHaveBeenCalledOnce();
    const signal = mocks.exportParsedVideos.mock.calls[0]![1] as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => release(result));
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("handles the native dialog Escape cancellation", () => {
    const { onClose } = renderDialog();
    const dialog = screen.getByRole("dialog");
    onClose.mockImplementation(() => { expect(dialog).not.toHaveAttribute("open"); });
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
