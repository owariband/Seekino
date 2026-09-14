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

export function formatTimecode(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return [hours, minutes, remainder]
    .filter((_, index) => hours > 0 || index > 0)
    .map((value) => String(value).padStart(2, "0"))
    .join(":");
}

export function formatTimeRange(startMs: number, endMs: number): string {
  return `${formatTimecode(startMs)}–${formatTimecode(endMs)}`;
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
  QUEUED: "等待处理",
  PREPARING: "媒体准备",
  SPLIT_READY: "切片完成",
  PARSING: "理解画面与对白",
  TEXT_EMBEDDING: "生成文本向量",
  VISION_EMBEDDING: "生成视觉向量",
  PERSISTING: "保存证据",
  INDEXING: "同步检索索引",
  COMPLETED: "可检索",
};

export function stageLabel(stage: string): string {
  return stageLabels[stage] ?? stage.replaceAll("_", " ").toLowerCase();
}

export function episodeLabel(order: number): string {
  return `EP ${String(order).padStart(2, "0")}`;
}

export function inferEpisodeOrder(filename: string, fallback: number): number {
  const stem = filename.replace(/\.[^.]+$/, "");
  const matches = [...stem.matchAll(/(?:ep(?:isode)?[\s._-]*)?(\d{1,3})/gi)];
  const candidate = matches.at(-1)?.[1];
  if (!candidate) return fallback;
  const parsed = Number(candidate);
  return parsed > 0 ? parsed : fallback;
}
