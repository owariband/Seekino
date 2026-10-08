import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatMoneyUsd,
  formatTimeRange,
  formatTimecode,
  inferEpisodeOrder,
  publicParseStageLabel,
  stageLabel,
} from "./format";

describe("format helpers", () => {
  it("preserves USD decimal precision without converting money to a floating point number", () => {
    expect(formatMoneyUsd("0.00000004")).toBe("$0.00000004");
    expect(formatMoneyUsd("9007199254740993.123456789012345678")).toBe("$9,007,199,254,740,993.123456789012345678");
    expect(formatMoneyUsd("1000.00000000")).toBe("$1,000.00000000");
    expect(formatMoneyUsd("0")).toBe("$0.00");
    expect(formatMoneyUsd(null)).toBe("未计价");
  });

  it("formats evidence time ranges consistently", () => {
    expect(formatTimecode(1_252)).toBe("20:52");
    expect(formatTimeRange(1_252, 1_259)).toBe("20:52–20:59");
  });

  it("extracts episode order while retaining a safe fallback", () => {
    expect(inferEpisodeOrder("series_EP_09.mp4", 1)).toBe(9);
    expect(inferEpisodeOrder("final-cut.mov", 12)).toBe(12);
  });

  it("uses human readable file sizes", () => {
    expect(formatBytes(1_073_741_824)).toBe("1.00 GB");
  });

  it("presents internal job stages as current activity", () => {
    expect(stageLabel("PARSING")).toBe("理解内容");
    expect(stageLabel("TEXT_EMBEDDING")).toBe("生成文本向量");
    expect(stageLabel("VISION_EMBEDDING")).toBe("生成视觉向量");
  });

  it("collapses internal implementation stages for product-facing progress", () => {
    expect(publicParseStageLabel("PARSING")).toBe("解析画面与对白");
    expect(publicParseStageLabel("TEXT_EMBEDDING")).toBe("发布检索结果");
    expect(publicParseStageLabel("VISION_EMBEDDING")).toBe("发布检索结果");
    expect(publicParseStageLabel("COMPLETED")).toBe("可检索");
  });
});
