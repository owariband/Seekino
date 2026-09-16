import type { ParseJobView, UploadSessionView } from "../../api/contracts";

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
  session?: UploadSessionView;
  job?: ParseJobView;
  error?: string;
}

export function taskProgress(task: UploadTask): number {
  if (task.status === "completed") return 100;
  if (task.status === "processing" && task.job) return task.job.progress;
  if (task.status === "completing") return 100;
  if (task.totalBytes <= 0) return 0;
  return Math.min(
    99,
    Math.round(((task.confirmedBytes + task.inFlightBytes) / task.totalBytes) * 100),
  );
}

export function taskStatusLabel(task: UploadTask): string {
  switch (task.status) {
    case "queued":
      return "等待上传";
    case "creating":
      return "建立传输会话";
    case "uploading":
      return "正在上传";
    case "paused":
      return "已暂停";
    case "completing":
      return "校验并合并";
    case "processing":
      return task.job?.stage ? `解析 · ${task.job.stage}` : "等待解析";
    case "completed":
      return "已可检索";
    case "failed":
      return "需要处理";
    case "cancelled":
      return "已删除上传任务";
    case "deleting":
      return "正在删除";
  }
}
