import { beforeEach, describe, expect, it, vi } from "vitest";

import { readVideoProgress, readWatchHistory, saveVideoProgress } from "./playbackProgress";

beforeEach(() => { localStorage.clear(); vi.useRealTimers(); });

describe("local playback progress", () => {
  it("keeps one latest entry per video and sorts recently watched videos first", () => {
    vi.spyOn(Date, "now").mockReturnValueOnce(100).mockReturnValueOnce(200).mockReturnValueOnce(300);
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionSeconds: 4, durationSeconds: 20 });
    saveVideoProgress({ seriesId: "series-2", videoId: "video-2", positionSeconds: 8, durationSeconds: 30 });
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionSeconds: 12, durationSeconds: 20 });
    expect(readWatchHistory().map((item) => [item.videoId, item.positionSeconds])).toEqual([["video-1", 12], ["video-2", 8]]);
    expect(readVideoProgress("video-2")?.seriesId).toBe("series-2");
    expect(readVideoProgress("unknown")).toBeNull();
    vi.restoreAllMocks();
  });

  it("migrates legacy millisecond progress to bounded integer seconds once", () => {
    localStorage.setItem("anivag-watch-progress:v1", JSON.stringify([
      { seriesId: "series-1", videoId: "video-1", positionMs: 12_999, durationMs: 20_001, updatedAt: 100 },
      { seriesId: "series-2", videoId: "video-2", positionMs: 99_999, durationMs: 20_001, updatedAt: 200 },
    ]));

    expect(readWatchHistory()).toEqual([
      { seriesId: "series-2", videoId: "video-2", positionSeconds: 21, durationSeconds: 21, updatedAt: 200 },
      { seriesId: "series-1", videoId: "video-1", positionSeconds: 12, durationSeconds: 21, updatedAt: 100 },
    ]);
    expect(localStorage.getItem("anivag-watch-progress:v1")).toBeNull();
    expect(localStorage.getItem("anivag-watch-progress:v2")).toContain('"positionSeconds":21');
  });

  it("ignores corrupt records and continues if browser storage is unavailable", () => {
    localStorage.setItem("anivag-watch-progress:v1", "{broken");
    expect(readWatchHistory()).toEqual([]);
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionSeconds: 99, durationSeconds: 20 });
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(20);
    vi.spyOn(Storage.prototype, "setItem").mockImplementationOnce(() => { throw new Error("quota"); });
    expect(() => saveVideoProgress({ seriesId: "series-2", videoId: "video-2", positionSeconds: 1, durationSeconds: 20 })).not.toThrow();
    vi.restoreAllMocks();
  });
});
