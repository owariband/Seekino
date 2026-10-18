import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatMoneyCny,
  formatMoneyUsd,
  formatTokens,
  episodeTitle,
  previewFramePosition,
  formatTimeRange,
  formatTimecode,
  formatTimeText,
  inferEpisodeOrder,
  publicParseStageLabel,
  stageLabel,
} from "./format";

describe("format helpers", () => {
  it("localizes generated labels without changing money precision or media timecodes", () => {
    expect(formatTokens(null, "en")).toBe("Not recorded");
    expect(formatTokens(12345, "en")).toBe("12,345");
    expect(formatMoneyUsd(null, "en")).toBe("Not priced");
    expect(formatMoneyUsd("0.00000004", "en")).toBe("$0.00000004");
    expect(episodeTitle(13, "en")).toBe("Episode 13");
    expect(episodeTitle(13)).toBe("第 13 集");
    expect(stageLabel("PARSING", "en")).toBe("Understanding content");
    expect(publicParseStageLabel("COMPLETED", "en")).toBe("Searchable");
    expect(formatTimeRange(8008, 8250)).toBe("00:08–00:08");
  });

  it("preserves USD decimal precision without converting money to a floating point number", () => {
    expect(formatMoneyUsd("0.00000004")).toBe("$0.00000004");
    expect(formatMoneyUsd("9007199254740993.123456789012345678")).toBe("$9,007,199,254,740,993.123456789012345678");
    expect(formatMoneyUsd("1000.00000000")).toBe("$1,000.00000000");
    expect(formatMoneyUsd("0")).toBe("$0.00");
    expect(formatMoneyUsd(null)).toBe("未计价");
  });

  it("rounds CNY estimates for display without floating point", () => {
    expect(formatMoneyCny("106.85095242")).toBe("¥106.85");
    expect(formatMoneyCny("1234567.995")).toBe("¥1,234,568.00");
    expect(formatMoneyCny("0.15")).toBe("¥0.15");
    expect(formatMoneyCny("0.00123456")).toBe("¥0.0012");
    expect(formatMoneyCny("0")).toBe("¥0.00");
    expect(formatMoneyCny("12")).toBe("¥12.00");
  });

  it("samples episode previews past the opening credits", () => {
    expect(previewFramePosition(1_430_000)).toBe(500_500);
    expect(previewFramePosition(0)).toBe(0);
  });

  it("formats evidence time ranges consistently", () => {
    expect(formatTimecode(1_252_000)).toBe("20:52");
    expect(formatTimeRange(1_252_000, 1_259_000)).toBe("20:52–20:59");
  });

  it("displays media milliseconds as whole seconds without rounding into the next second", () => {
    expect(formatTimecode(8_008)).toBe("00:08");
    expect(formatTimecode(8_250)).toBe("00:08");
    expect(formatTimecode(8_100)).toBe("00:08");
    expect(formatTimecode(8_000)).toBe("00:08");
    expect(formatTimecode(59_999)).toBe("00:59");
    expect(formatTimecode(3_599_999)).toBe("59:59");
    expect(formatTimecode(3_600_001)).toBe("60:00");
    expect(formatTimecode(100)).toBe("00:00");
    expect(formatTimecode(0)).toBe("00:00");
    expect(formatTimeRange(8_008, 8_250)).toBe("00:08–00:08");
  });

  it("formats explicit times in display text without treating other numbers as timestamps", () => {
    expect(formatTimeText("第13集 09:23.400–09:57.500；01:02:03.004；10分38.456秒")).toBe("第13集 09:23–09:57；62:03；10:38");
    expect(formatTimeText("See 09:23.400.")).toBe("See 09:23.");
    expect(formatTimeText("片段[540000,546800) 回忆发生在599300毫秒")).toBe("片段09:00–09:06 回忆发生在09:59");
    expect(formatTimeText("函数定义域为[0,10)，概率范围[0,1)，读取字符范围[540000,546800)。")).toBe("函数定义域为[0,10)，概率范围[0,1)，读取字符范围[540000,546800)。");
    expect(formatTimeText("16:9，2026 年，价格 12.50，编号 599300，错误时间 01:60:00")).toBe("16:9，2026 年，价格 12.50，编号 599300，错误时间 01:60:00");
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
