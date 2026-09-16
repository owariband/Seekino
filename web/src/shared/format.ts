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

export function formatCostCny(value: string | null): string {
  if (value === null) return "未估算";
  const [integer, fraction = ""] = value.split(".");
  return `¥${integer!.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${fraction.padEnd(2, "0")}`;
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

export function inferEpisodeOrder(filename: string, fallback: number): number {
  const stem = filename.replace(/\.[^.]+$/, "");
  const matches = [...stem.matchAll(/(?:ep(?:isode)?[\s._-]*)?(\d{1,3})/gi)];
  const candidate = matches.at(-1)?.[1];
  if (!candidate) return fallback;
  const parsed = Number(candidate);
  return parsed > 0 ? parsed : fallback;
}
