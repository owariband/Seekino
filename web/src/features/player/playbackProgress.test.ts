import { beforeEach, describe, expect, it, vi } from "vitest";

import { readVideoProgress, readWatchHistory, saveVideoProgress } from "./playbackProgress";

beforeEach(() => { localStorage.clear(); vi.useRealTimers(); });

describe("local playback progress", () => {
  it("keeps one latest entry per video and sorts recently watched videos first", () => {
    vi.spyOn(Date, "now").mockReturnValueOnce(100).mockReturnValueOnce(200).mockReturnValueOnce(300);
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionMs: 4_000, durationMs: 20_000 });
    saveVideoProgress({ seriesId: "series-2", videoId: "video-2", positionMs: 8_000, durationMs: 30_000 });
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionMs: 12_000, durationMs: 20_000 });
    expect(readWatchHistory().map((item) => [item.videoId, item.positionMs])).toEqual([["video-1", 12_000], ["video-2", 8_000]]);
    expect(readVideoProgress("video-2")?.seriesId).toBe("series-2");
    expect(readVideoProgress("unknown")).toBeNull();
    vi.restoreAllMocks();
  });

  it("ignores corrupt records and continues if browser storage is unavailable", () => {
    localStorage.setItem("anivag-watch-progress:v1", "{broken");
    expect(readWatchHistory()).toEqual([]);
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionMs: 99_000, durationMs: 20_000 });
    expect(readVideoProgress("video-1")?.positionMs).toBe(20_000);
    vi.spyOn(Storage.prototype, "setItem").mockImplementationOnce(() => { throw new Error("quota"); });
    expect(() => saveVideoProgress({ seriesId: "series-2", videoId: "video-2", positionMs: 1_000, durationMs: 20_000 })).not.toThrow();
    vi.restoreAllMocks();
  });
});
