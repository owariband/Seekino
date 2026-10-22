import { describe, expect, it } from "vitest";

import type { ParseConfig } from "../../api/contracts";
import type { UploadTask } from "./upload-types";
import { mediaTypeForFile, taskProgress, taskStatusLabel, validateParseConfig } from "./upload-types";

const parseDefaults: ParseConfig = {
  version: 2,
  window: { chunkDurationSeconds: 60, contextOverlapSeconds: 3, analysisMaxHeight: 720, canonicalMaxHeight: 720 },
  asr: { mode: "lite", model: "lite", fps: 1, maxOutputTokens: null, concurrency: 4 },
  vision: { model: "pro", inputMode: "video", fps: 1, maxFramesPerWindow: null, maxOutputTokens: 65536 },
  context: { maxEvents: 8, maxChars: 2000, backgroundTextEnabled: true, backgroundTextMaxChars: null,
    backgroundImagesEnabled: true, maxBackgroundImages: null, backgroundImageMaxEdge: 1024 },
  fallback: { enabled: true, model: "flash", frameFps: 0.5, groupDurationSeconds: 30, maxFramesPerGroup: 15,
    maxHeight: 720, reasoningEffort: "none", maxOutputTokens: 8192, requestTimeoutSeconds: 1200 },
  embedding: { model: "embedding", dimensions: 1024, videoFps: 1 },
  execution: { requestTimeoutSeconds: 600, maxAttempts: 3 },
};

it.each([
  ["vision", 65536, null], ["vision", 262144, null],
  ["vision", 262145, "主解析最大输出 Token"],
  ["asr", 32768, null], ["asr", 32769, "ASR 最大输出 Token"],
  ["fallback", 32768, null], ["fallback", 32769, "兜底最大输出 Token"],
] as const)("validates %s output budget %s without changing other stage limits", (stage, budget, error) => {
  const config = { ...parseDefaults, [stage]: { ...parseDefaults[stage], maxOutputTokens: budget } };
  const problem = validateParseConfig(config, parseDefaults);
  if (error === null) expect(problem).toBeNull();
  else expect(problem).toContain(error);
});

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

  it("uses the persisted V2 task status without translating it through a V1 job", () => {
    const videoParseTask = {
      videoParseTaskId: "task-1", videoId: "video-1", pipelineVersion: "video-parse-v2",
      status: "INDEXING" as const, parseConfig: {}, chunkCount: 3, version: 1,
      nextExecuteTime: 1, attemptCount: 1, maxAttempts: 3, leaseOwner: "worker-1",
      leaseVersion: 1, leaseExpireTime: 3, heartbeatTime: 2, startTime: 1,
      finishTime: null, processingDurationMs: 10, processingTimingComplete: false,
      lastError: null, isTest: false, detail: {}, createTime: 1, updateTime: 2,
      tenantId: null, seriesId: null, seriesName: null, episodeOrder: null,
      originalFilename: null, uploadSessionId: null, durationSeconds: null,
      canRetry: false, canReparse: false, canDelete: false,
    };
    const processing = task({ status: "processing", videoParseTask });

    expect(taskProgress(processing)).toBe(100);
    expect(taskProgress(task({ status: "failed", videoParseTask: { ...videoParseTask, status: "PARSING_FAILED" } }))).toBe(100);
    expect(taskStatusLabel(task({ status: "paused" }))).toBe("已暂停");
    expect(taskStatusLabel(processing)).toBe("发布检索结果");
    expect(taskStatusLabel(task({
      status: "failed",
      videoParseTask: { ...videoParseTask, status: "INDEXING_FAILED" },
    }))).toBe("需要处理");
  });

  it("normalizes only media types accepted by the upload API", () => {
    expect(mediaTypeForFile(new File([], "episode.MOV"))).toBe("video/quicktime");
    expect(mediaTypeForFile(new File([], "episode.mp4", { type: "video/mp4" }))).toBe("video/mp4");
    expect(mediaTypeForFile(new File([], "notes.txt", { type: "text/plain" }))).toBeNull();
  });
});
