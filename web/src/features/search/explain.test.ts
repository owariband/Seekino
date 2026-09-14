import { describe, expect, it } from "vitest";

import type { VideoChunkSearchHit } from "../../api/contracts";
import { explainHit, readableTranscript } from "./explain";

const hit: VideoChunkSearchHit = {
  candidate: {
    videoChunkId: "chunk-1",
    fusedRank: 1,
    fusedScore: 0.032,
    textRank: 1,
    textScore: 0.91,
    visionRank: 2,
    visionScore: 0.84,
  },
  selectionReason: {
    summary: "对白中的等待与画面中的雨伞在同一时间片出现，因此优先核验。",
    matchedEvidence: [
      {
        kind: "TRANSCRIPT",
        text: "我们在门口等你。",
        startMs: 12_000,
        endMs: 14_000,
      },
      {
        kind: "VISUAL_EVENT",
        text: "人物手持雨伞站在门口。",
        startMs: 12_000,
        endMs: 15_000,
      },
    ],
    limitations: [],
  },
  evidence: {
    seriesId: "series-1",
    workspaceId: "local-default",
    seriesName: "测试系列",
    seriesDescription: null,
    seriesStatus: "READY",
    importantExtraData: null,
    characterBible: [],
    videoId: "video-1",
    episodeOrder: 1,
    originalFilename: "episode-01.mp4",
    originalSourceUri: "artifact://source/video-1",
    inferenceProxyUri: null,
    contentHash: "hash",
    durationMs: 60_000,
    uploadStatus: "COMPLETED",
    parseStatus: "SUCCEEDED",
    videoChunkId: "chunk-1",
    sequenceNo: 0,
    startMs: 12_000,
    endMs: 15_000,
    analysisWindowStartMs: 10_000,
    analysisWindowEndMs: 18_000,
    chunkMediaRef: "artifact://chunks/chunk-1",
    analysisWindowMediaRef: "artifact://windows/chunk-1",
    transcript: [
      { speaker: "角色甲", text: "我们在门口等你。", startMs: 12_000, endMs: 14_000 },
    ],
    baselineVisualCaption: "人物手持雨伞站在门口。",
    keyframeUris: [],
    allTimeEvents: [],
    allTimeEventText: "人物手持雨伞站在门口。",
    retrievalText: "门口 等待 雨伞",
    videoCompact: "雨天，人物在门口等待。",
    contextBeforeCompact: "室外开始下雨。",
    contextAfterCompact: "另一人从走廊出现。",
    windowSummary: "一场发生在门口的等待。",
    modelProvider: "provider",
    modelName: "model",
    pipelineVersion: "m1",
    isFallback: false,
    parseProvenance: null,
    parsedAt: "2026-09-15T00:00:00Z",
  },
};

describe("search evidence explanations", () => {
  it("keeps the public rationale tied to returned API evidence", () => {
    const explanation = explainHit(hit);

    expect(explanation.summary).toContain("雨伞");
    expect(explanation.matchedFacts).toContain("我们在门口等你。");
    expect(explanation.sources).toEqual(
      expect.arrayContaining(["对白文本 · 文本召回第 1", "画面事件 · 视觉召回第 2"]),
    );
    expect(readableTranscript(hit)).toContain("我们在门口等你");
  });

  it("preserves an API-provided missing-channel limitation", () => {
    const visualOnly: VideoChunkSearchHit = {
      ...hit,
      candidate: { ...hit.candidate, textRank: null, textScore: null },
      selectionReason: {
        ...hit.selectionReason,
        limitations: ["文本通道没有召回此片段。"],
      },
    };
    expect(
      explainHit(visualOnly).limitations.some((item) => item.includes("文本通道没有召回")),
    ).toBe(true);
  });
});
