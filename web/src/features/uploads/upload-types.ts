import type {
  ParseConfig,
  ParseJobView,
  UploadSessionView,
  VideoParseTaskStatus,
  VideoParseTaskView,
} from "../../api/contracts";
import { translate, type Locale } from "../../shared/messages";
import { publicParseStageLabel } from "../../shared/format";

export type UploadTaskStatus =
  | "queued"
  | "creating"
  | "uploading"
  | "paused"
  | "completing"
  | "processing"
  | "completed"
  | "failed"
  | "deleting"
  | "cancelled";

export interface UploadInput {
  file: File;
  episodeOrder: number;
  parseConfig?: ParseConfig;
}

const supportedMediaTypes = new Set([
  "video/mp4",
  "video/quicktime",
  "video/x-msvideo",
]);

const mediaTypeByExtension: Record<string, string> = {
  mp4: "video/mp4",
  mov: "video/quicktime",
  avi: "video/x-msvideo",
};

export function mediaTypeForFile(file: File): string | null {
  const normalizedType = file.type.toLowerCase();
  if (supportedMediaTypes.has(normalizedType)) return normalizedType;
  const extension = file.name.split(".").at(-1)?.toLowerCase();
  return extension ? (mediaTypeByExtension[extension] ?? null) : null;
}

export interface UploadTask {
  taskId: string;
  seriesId: string;
  file?: File;
  fileName: string;
  episodeOrder: number;
  totalBytes: number;
  confirmedBytes: number;
  inFlightBytes: number;
  status: UploadTaskStatus;
  idempotencyKey: string;
  parseConfig?: ParseConfig;
  session?: UploadSessionView;
  job?: ParseJobView;
  videoParseTask?: VideoParseTaskView;
  error?: string;
}

const videoParseTaskStatusLabel: Record<VideoParseTaskStatus, string> = {
  PENDING: "等待解析",
  PREPARING: "准备解析",
  PARSING: "解析画面与对白",
  INDEXING: "发布检索结果",
  COMPLETE: "已可检索",
  PARSING_FAILED: "解析失败",
  INDEXING_FAILED: "索引发布失败",
};

export function configuredMin(recommended: number, current: number): number {
  return Math.min(recommended, current);
}

export function configuredMax(recommended: number, current: number): number {
  return Math.max(recommended, current);
}

export function validateParseConfig(config: ParseConfig, defaults: ParseConfig, locale: Locale = "zh"): string | null {
  const fields: [string, number | null, number, number, boolean?][] = [
    ["每段时长", config.window.chunkDurationSeconds, configuredMin(1, defaults.window.chunkDurationSeconds), configuredMax(120, defaults.window.chunkDurationSeconds), true],
    ["上下文重叠", config.window.contextOverlapSeconds, 0, configuredMax(30, defaults.window.contextOverlapSeconds), true],
    ["解析输入高度", config.window.analysisMaxHeight, configuredMin(64, defaults.window.analysisMaxHeight), configuredMax(2160, defaults.window.analysisMaxHeight), true],
    ["回放与入库高度", config.window.canonicalMaxHeight, configuredMin(64, defaults.window.canonicalMaxHeight), configuredMax(2160, defaults.window.canonicalMaxHeight), true],
    ["ASR FPS", config.asr.fps, 0.2, 5],
    ["ASR 最大输出 Token", config.asr.maxOutputTokens, 256, 32768, true],
    ["ASR 并发", config.asr.concurrency, 1, configuredMax(16, defaults.asr.concurrency), true],
    ["主解析 FPS", config.vision.fps, 0.2, 5],
    ["每段最多帧数", config.vision.maxFramesPerWindow, 1, 600, true],
    ["主解析最大输出 Token", config.vision.maxOutputTokens, 256, 262144, true],
    ["前序事件数", config.context.maxEvents, 1, configuredMax(100, defaults.context.maxEvents), true],
    ["前序上下文字数", config.context.maxChars, 256, configuredMax(100000, defaults.context.maxChars), true],
    ["背景文字最多字符", config.context.backgroundTextMaxChars, 256, 200000, true],
    ["最多背景参考图", config.context.maxBackgroundImages, 1, 64, true],
    ["背景参考图最大边长", config.context.backgroundImageMaxEdge, configuredMin(64, defaults.context.backgroundImageMaxEdge), defaults.context.backgroundImageMaxEdge, true],
    ["兜底抽帧 FPS", config.fallback.frameFps, 0.2, 5],
    ["兜底分组时长", config.fallback.groupDurationSeconds, 1, 120, true],
    ["兜底每组最多帧数", config.fallback.maxFramesPerGroup, 1, 128, true],
    ["兜底图片高度", config.fallback.maxHeight, 64, 2160, true],
    ["兜底最大输出 Token", config.fallback.maxOutputTokens, 256, 32768, true],
    ["兜底超时", config.fallback.requestTimeoutSeconds, configuredMin(0.01, defaults.fallback.requestTimeoutSeconds), configuredMax(3600, defaults.fallback.requestTimeoutSeconds)],
    ["视频向量化 FPS", config.embedding.videoFps, 0.2, 5],
    ["模型请求超时", config.execution.requestTimeoutSeconds, configuredMin(0.01, defaults.execution.requestTimeoutSeconds), configuredMax(3600, defaults.execution.requestTimeoutSeconds)],
    ["最多尝试次数", config.execution.maxAttempts, 1, configuredMax(10, defaults.execution.maxAttempts), true],
  ];
  for (const [label, value, minimum, maximum, integer] of fields) {
    if (value === null) continue;
    if (!Number.isFinite(value) || value < minimum || value > maximum || (integer && !Number.isInteger(value))) {
      return translate(locale, "{label}必须在 {minimum}–{maximum} 之间{integer}", { label: translate(locale, label), minimum, maximum, integer: integer ? translate(locale, "，且为整数") : "" });
    }
  }
  if (config.vision.inputMode === "ordered_frames" && config.vision.maxFramesPerWindow === null) {
    return translate(locale, "逐帧解析需要设置每段最多帧数");
  }
  if (config.vision.inputMode === "video" && config.vision.maxFramesPerWindow !== null) {
    return translate(locale, "直传视频不能设置逐帧上限");
  }
  if (![config.vision.model, config.asr.model, config.fallback.model].every((model) => model.trim())) {
    return translate(locale, "模型 ID 不能为空");
  }
  if (config.embedding.model !== defaults.embedding.model || config.embedding.dimensions !== defaults.embedding.dimensions) {
    return translate(locale, "Embedding 模型与维度必须和当前索引一致");
  }
  return null;
}

export function taskProgress(task: UploadTask): number {
  if (
    ["completing", "processing", "completed"].includes(task.status)
    || (task.status === "failed" && (task.job || task.videoParseTask))
  ) return 100;
  if (task.totalBytes <= 0) return 0;
  return Math.min(
    99,
    Math.round(((task.confirmedBytes + task.inFlightBytes) / task.totalBytes) * 100),
  );
}

export function taskStatusLabel(task: UploadTask, locale: Locale = "zh"): string {
  switch (task.status) {
    case "queued":
      return translate(locale, "等待上传");
    case "creating":
      return translate(locale, "建立传输会话");
    case "uploading":
      return translate(locale, "正在上传");
    case "paused":
      return translate(locale, "已暂停");
    case "completing":
      return translate(locale, "校验并合并");
    case "processing":
      if (task.videoParseTask) {
        return translate(locale, videoParseTaskStatusLabel[task.videoParseTask.status]);
      }
      return task.job?.stage ? publicParseStageLabel(task.job.stage, locale) : translate(locale, "等待解析");
    case "completed":
      return translate(locale, "已可检索");
    case "failed":
      return translate(locale, "需要处理");
    case "cancelled":
      return translate(locale, "已删除上传任务");
    case "deleting":
      return translate(locale, "正在删除");
  }
}
