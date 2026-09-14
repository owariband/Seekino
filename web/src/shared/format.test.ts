import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatTimeRange,
  formatTimecode,
  inferEpisodeOrder,
} from "./format";

describe("format helpers", () => {
  it("formats evidence time ranges consistently", () => {
    expect(formatTimecode(1_252_000)).toBe("20:52");
    expect(formatTimeRange(1_252_000, 1_259_000)).toBe("20:52–20:59");
  });

  it("extracts episode order while retaining a safe fallback", () => {
    expect(inferEpisodeOrder("series_EP_09.mp4", 1)).toBe(9);
    expect(inferEpisodeOrder("final-cut.mov", 12)).toBe(12);
  });

  it("uses human readable file sizes", () => {
    expect(formatBytes(1_073_741_824)).toBe("1.00 GB");
  });
});
