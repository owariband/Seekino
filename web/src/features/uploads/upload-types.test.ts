import { describe, expect, it } from "vitest";

import type { UploadTask } from "./upload-types";
import { mediaTypeForFile, taskProgress, taskStatusLabel } from "./upload-types";

function task(overrides: Partial<UploadTask> = {}): UploadTask {
  return {
    taskId: "upload-1",
    seriesId: "series-1",
    fileName: "episode-01.mp4",
    episodeOrder: 1,
    totalBytes: 1_000,
    confirmedBytes: 400,
    inFlightBytes: 120,
    status: "uploading",
    idempotencyKey: "stable-key",
    ...overrides,
  };
}

describe("upload presentation state", () => {
  it("includes in-flight bytes but never claims completion early", () => {
    expect(taskProgress(task())).toBe(52);
    expect(taskProgress(task({ confirmedBytes: 1_000 }))).toBe(99);
  });

  it("switches processing progress to the parse job", () => {
    expect(
      taskProgress(
        task({
          status: "processing",
          job: {
            jobId: "job-1", workspaceId: "local-default", retryOfJobId: null, version: 1, estimatedRemainingMs: null, canRetry: false, canCancel: false,
            seriesId: "series-1",
            videoId: "video-1",
            seriesName: "测试系列",
            episodeOrder: 1,
            originalFilename: "episode-01.mp4",
            pipelineVersion: "m1",
            status: "RUNNING",
            stage: "PARSING",
            progress: 37,
            attemptCount: 1,
            maxAttempts: 3,
            availableAt: Date.parse("2026-09-14T00:00:00Z"),
            startedAt: null,
            completedAt: null,
            lastError: null,
            checkpoints: [],
            createdAt: Date.parse("2026-09-14T00:00:00Z"),
            updatedAt: Date.parse("2026-09-14T00:00:00Z"),
          },
        }),
      ),
    ).toBe(37);
    expect(taskStatusLabel(task({ status: "paused" }))).toBe("已暂停");
  });

  it("normalizes only media types accepted by the upload API", () => {
    expect(mediaTypeForFile(new File([], "episode.MOV"))).toBe("video/quicktime");
    expect(mediaTypeForFile(new File([], "episode.mp4", { type: "video/mp4" }))).toBe("video/mp4");
    expect(mediaTypeForFile(new File([], "notes.txt", { type: "text/plain" }))).toBeNull();
  });
});
