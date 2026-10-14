export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  );
  const value = bytes / 1024 ** index;
  const digits = index >= 3 ? 2 : index === 0 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[index]}`;
}

export function formatTokens(value: number | null): string {
  return value === null ? "未记录" : value.toLocaleString("zh-CN");
}

export function formatMoneyUsd(value: string | null): string {
  if (value === null) return "未计价";
  const [integer, fraction = ""] = value.split(".");
  return `$${integer!.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${fraction.padEnd(2, "0")}`;
}

/**
 * Rounds a decimal-string CNY estimate for display without floating point: two decimals,
 * or up to four for sub-yuan amounts so small costs do not collapse to ¥0.00.
 */
export function formatMoneyCny(value: string): string {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return `¥${value}`;
  const [, sign, integer, fraction = ""] = match;
  const digits = /^0+$/.test(integer!) ? 4 : 2;
  const roundUp = Number(fraction[digits] ?? "0") >= 5 ? 1n : 0n;
  const scaled = (BigInt(integer! + fraction.padEnd(digits, "0").slice(0, digits)) + roundUp).toString().padStart(digits + 1, "0");
  const whole = scaled.slice(0, -digits).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const cents = scaled.slice(-digits).replace(/0+$/, "").padEnd(2, "0");
  return `¥${sign}${whole}.${cents}`;
}

/**
 * Integer milliseconds for an episode preview frame. Early frames are mostly opening credits
 * or title cards in episodic video, so previews sample the middle of the first half instead.
 */
export function previewFramePosition(durationMilliseconds: number): number {
  return Math.round(Math.max(0, durationMilliseconds) * 0.35);
}

/** Display media milliseconds as whole minutes:seconds without changing playback precision. */
export function formatTimecode(milliseconds: number): string {
  const seconds = Number.isFinite(milliseconds) ? Math.floor(Math.max(0, milliseconds) / 1000) : 0;
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

/** The retained Seconds parameter names carry integer milliseconds. */
export function formatTimeRange(startSeconds: number, endSeconds: number): string {
  return `${formatTimecode(startSeconds)}–${formatTimecode(endSeconds)}`;
}

// Recognize explicit media timestamps, including fractional seconds; ratios such as 16:9 do not match.
export const MEDIA_TIMESTAMP_PATTERN = /(?:(\d{1,2})\s*小时\s*)?(\d{1,3})\s*分(?:钟)?\s*(\d{1,2}(?:\.\d{1,3})?)\s*秒|(?<![\d:])(?:(\d{1,2}):)?(\d{1,3}):(\d{2}(?:\.\d{1,3})?)(?![\d:]|\.\d)/g;

export function timestampMilliseconds(match: readonly (string | undefined)[]): number | null {
  const [, hours, minutes, seconds, clockHours, clockMinutes, clockSeconds] = match;
  const h = Number(hours ?? clockHours ?? 0);
  const m = Number(minutes ?? clockMinutes);
  const sec = Number(seconds ?? clockSeconds);
  if (sec >= 60 || ((hours ?? clockHours) !== undefined && m >= 60)) return null;
  return Math.round(((h * 60 + m) * 60 + sec) * 1000);
}

/** Format explicit time notation in display text, never API values or Markdown link destinations. */
export function formatTimeText(value: string): string {
  return value.replace(MEDIA_TIMESTAMP_PATTERN, (...match) => {
    const milliseconds = timestampMilliseconds(match);
    return milliseconds === null ? match[0] : formatTimecode(milliseconds);
  }).replace(/((?:视频)?片段(?:范围)?[：:\s]*|时间范围[：:\s]*)\[(\d+)\s*,\s*(\d+)\)/g, (match, label, start, end) => {
    return Number.isSafeInteger(Number(start)) && Number.isSafeInteger(Number(end)) && Number(end) > Number(start)
      ? `${label}${formatTimeRange(Number(start), Number(end))}` : match;
  }).replace(/(?<![\d.])(\d+)\s*毫秒/g, (match, milliseconds) => {
    return Number.isSafeInteger(Number(milliseconds)) ? formatTimecode(Number(milliseconds)) : match;
  });
}

export function clampPercentage(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

export function clipText(value: string, limit = 150): string {
  const compact = value.replace(/\s+/g, " ").trim();
  if (compact.length <= limit) return compact;
  return `${compact.slice(0, limit).trimEnd()}…`;
}

const stageLabels: Record<string, string> = {
  QUEUED: "等待开始建库",
  PREPARING: "准备媒体",
  SPLIT_READY: "完成媒体准备",
  PARSING: "理解内容",
  TEXT_EMBEDDING: "生成文本向量",
  VISION_EMBEDDING: "生成视觉向量",
  PERSISTING: "保存解析结果",
  INDEXING: "建立检索索引",
  COMPLETED: "已可检索",
};

export function stageLabel(stage: string): string {
  return stageLabels[stage] ?? stage.replaceAll("_", " ").toLowerCase();
}

const publicParseStageLabels: Record<string, string> = {
  QUEUED: "解析画面与对白",
  PREPARING: "解析画面与对白",
  SPLIT_READY: "解析画面与对白",
  PARSING: "解析画面与对白",
  TEXT_EMBEDDING: "发布检索结果",
  VISION_EMBEDDING: "发布检索结果",
  PERSISTING: "发布检索结果",
  INDEXING: "发布检索结果",
  COMPLETED: "可检索",
};

export function publicParseStageLabel(stage: string): string {
  return publicParseStageLabels[stage] ?? "解析画面与对白";
}

export function episodeLabel(order: number): string {
  return `EP ${String(order).padStart(2, "0")}`;
}

/** Viewer-facing episode title; the source filename belongs in tooltips and management views. */
export function episodeTitle(order: number): string {
  return `第 ${order} 集`;
}

export function seriesTitle(series: { name: string; displayName?: string | null }): string {
  return series.displayName?.trim() || series.name;
}

export function inferEpisodeOrder(filename: string, fallback: number): number {
  const stem = filename.replace(/\.[^.]+$/, "");
  const matches = [...stem.matchAll(/(?:ep(?:isode)?[\s._-]*)?(\d{1,3})/gi)];
  const candidate = matches.at(-1)?.[1];
  if (!candidate) return fallback;
  const parsed = Number(candidate);
  return parsed > 0 ? parsed : fallback;
}
