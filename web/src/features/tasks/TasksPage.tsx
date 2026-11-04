import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Activity,
  Pause,
  Play,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clock3,
  RefreshCw,
  RotateCcw,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useI18n } from "../../app/i18n";
import { translate, type Locale } from "../../shared/messages";
import {
  deleteUploadSession,
  deleteVideoParseHistory,
  deleteVideoParseVersion,
  errorMessage,
  listAllVideoParseTasks,
  reparseUpload,
  retryUploadParse,
  setVideoParsePaused,
  shouldRetryApiRequest,
} from "../../api/client";
import type {
  VideoParseTaskStatus,
  VideoParseTaskView,
} from "../../api/contracts";
import { useWorkspace } from "../../app/workspace";
import { episodeLabel, episodeTitle, formatTimecode as formatDuration } from "../../shared/format";
import { useUploads } from "../uploads/UploadProvider";
import { ParseUsageDetails } from "./ParseUsageDetails";
import styles from "./tasks.module.css";

type Filter = "inProgress" | "needsAttention" | "completed" | "all";

function isPaused(task: VideoParseTaskView): boolean {
  return task.isPaused ?? task.detail.execution_control?.paused ?? false;
}

const PIPELINE = ["解析画面与对白", "发布检索结果", "可检索"];

const PIPELINE_STAGE_INDEX: Record<VideoParseTaskStatus, number> = {
  PENDING: 0,
  PREPARING: 0,
  PARSING: 0,
  PARSING_FAILED: 0,
  INDEXING: 1,
  INDEXING_FAILED: 1,
  COMPLETE: 2,
};

const statusText: Record<VideoParseTaskStatus, string> = {
  PENDING: "排队中",
  PREPARING: "准备中",
  PARSING: "解析中",
  INDEXING: "发布索引中",
  COMPLETE: "已完成",
  PARSING_FAILED: "解析异常",
  INDEXING_FAILED: "索引异常",
};

const activeStatuses = new Set<VideoParseTaskStatus>([
  "PREPARING",
  "PARSING",
  "INDEXING",
]);

// The 进行中 filter includes PENDING as well; "active" below remains the stage notion
// (a PENDING task has not started yet and is not "running").
const inProgressStatuses = new Set<VideoParseTaskStatus>([
  "PENDING",
  "PREPARING",
  "PARSING",
  "INDEXING",
]);

function processingDurationText(task: VideoParseTaskView, locale: Locale): string {
  const t = (message: string) => translate(locale, message);
  const timing = task.detail.timing;
  if (timing) {
    if (!timing.recorded || timing.effectiveDurationMs === null) {
      return task.status === "PENDING" ? t("未开始") : t("未记录");
    }
    return formatDuration(timing.effectiveDurationMs);
  }
  if (task.attemptCount === 0) return t("未开始");
  if (task.processingDurationMs === 0) return t("未记录");
  return formatDuration(task.processingDurationMs);
}

function formatCreateTime(timestamp: number, formatLocale: string): string {
  return new Date(timestamp).toLocaleString(formatLocale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function mergeTaskProjection(
  current: VideoParseTaskView,
  updated: VideoParseTaskView,
): VideoParseTaskView {
  return {
    ...current,
    ...updated,
    tenantId: updated.tenantId ?? current.tenantId,
    seriesId: updated.seriesId ?? current.seriesId,
    seriesName: updated.seriesName ?? current.seriesName,
    episodeOrder: updated.episodeOrder ?? current.episodeOrder,
    originalFilename: updated.originalFilename ?? current.originalFilename,
    uploadSessionId: updated.uploadSessionId ?? current.uploadSessionId,
    durationSeconds: updated.durationSeconds ?? current.durationSeconds,
    canRetry: updated.canRetry,
    canReparse: updated.canReparse,
    canDelete: updated.canDelete,
    canDeleteVersion: updated.canDeleteVersion ?? current.canDeleteVersion,
    isSuperseded: updated.isSuperseded ?? current.isSuperseded,
  };
}

export function TasksPage() {
  const { t, locale, formatLocale } = useI18n();
  const { displayMode } = useWorkspace();
  const isDebug = displayMode === "debug";
  const { forgetDeletedSession, openDialog, syncRequeuedParse } = useUploads();
  const queryClient = useQueryClient();
  const [controllingTaskIds, setControllingTaskIds] = useState<ReadonlySet<string>>(() => new Set());
  const [filter, setFilter] = useState<Filter>("inProgress");
  const [initialFilterResolved, setInitialFilterResolved] = useState(false);
  // Videos seen in progress during this visit stay under 进行中 after they finish or fail,
  // so a card being watched reaches its final state instead of vanishing.
  const seenInProgressVideoIds = useRef(new Set<string>());
  const [deletingUploadSessionIds, setDeletingUploadSessionIds] = useState<ReadonlySet<string>>(() => new Set());
  const [retryingUploadSessionIds, setRetryingUploadSessionIds] = useState<ReadonlySet<string>>(() => new Set());
  const [reparsingUploadSessionIds, setReparsingUploadSessionIds] = useState<ReadonlySet<string>>(() => new Set());
  const [confirmingDeleteTaskId, setConfirmingDeleteTaskId] = useState<string | null>(null);
  const [confirmingReparseTaskId, setConfirmingReparseTaskId] = useState<string | null>(null);
  const [expandedHistoryVideoIds, setExpandedHistoryVideoIds] = useState<ReadonlySet<string>>(() => new Set());
  const [confirmingDeleteVersionId, setConfirmingDeleteVersionId] = useState<string | null>(null);
  const [confirmingDeleteHistoryVideoId, setConfirmingDeleteHistoryVideoId] = useState<string | null>(null);
  const [deletingVersionIds, setDeletingVersionIds] = useState<ReadonlySet<string>>(() => new Set());
  const [deletingHistoryVideoIds, setDeletingHistoryVideoIds] = useState<ReadonlySet<string>>(() => new Set());
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const [historyErrors, setHistoryErrors] = useState<Record<string, string>>({});
  const tasksQuery = useQuery({
    queryKey: ["video-parse-tasks"],
    queryFn: ({ signal }) => listAllVideoParseTasks(signal),
    retry: shouldRetryApiRequest,
    refetchInterval: 5_000,
  });

  const tasks = tasksQuery.data ?? [];
  const tasksByVideo = new Map<string, VideoParseTaskView[]>();
  for (const task of tasks) {
    const list = tasksByVideo.get(task.videoId);
    if (list) list.push(task);
    else tasksByVideo.set(task.videoId, [task]);
  }
  const latestVersionByVideo = new Map<string, number>();
  for (const [videoId, list] of tasksByVideo) {
    let latest = 0;
    for (const task of list) latest = Math.max(latest, task.version);
    latestVersionByVideo.set(videoId, latest);
  }
  const latestTasks = tasks.filter((task) => task.version === latestVersionByVideo.get(task.videoId));
  const historyByVideo = new Map<string, VideoParseTaskView[]>();
  for (const [videoId, list] of tasksByVideo) {
    const latest = latestVersionByVideo.get(videoId)!;
    historyByVideo.set(videoId, list
      .filter((task) => task.version !== latest)
      .sort((a, b) => b.version - a.version));
  }

  const inProgressCount = latestTasks.filter((task) => inProgressStatuses.has(task.status)).length;
  const needsAttentionCount = latestTasks.filter((task) => task.status === "PARSING_FAILED" || task.status === "INDEXING_FAILED").length;
  const completedCount = latestTasks.filter((task) => task.status === "COMPLETE").length;
  const filterCounts: Record<Filter, number> = {
    inProgress: inProgressCount,
    needsAttention: needsAttentionCount,
    completed: completedCount,
    all: latestTasks.length,
  };

  // On the first successful load only, fall back to 全部 when nothing is in progress.
  useEffect(() => {
    if (initialFilterResolved || !tasksQuery.isSuccess) return;
    setInitialFilterResolved(true);
    if (inProgressCount === 0) setFilter("all");
  }, [initialFilterResolved, tasksQuery.isSuccess, inProgressCount]);

  for (const task of latestTasks) {
    if (inProgressStatuses.has(task.status)) seenInProgressVideoIds.current.add(task.videoId);
  }
  const visibleTasks = filter === "all"
    ? latestTasks
    : latestTasks.filter((task) => {
      if (filter === "inProgress") return seenInProgressVideoIds.current.has(task.videoId);
      if (filter === "needsAttention") return task.status === "PARSING_FAILED" || task.status === "INDEXING_FAILED";
      return task.status === "COMPLETE";
    });

  const toggleHistory = (videoId: string) => {
    setExpandedHistoryVideoIds((current) => {
      const next = new Set(current);
      if (next.has(videoId)) next.delete(videoId);
      else next.add(videoId);
      return next;
    });
  };

  const togglePause = async (task: VideoParseTaskView) => {
    const id = task.videoParseTaskId;
    if (controllingTaskIds.has(id)) return;
    setControllingTaskIds((current) => new Set(current).add(id));
    setActionErrors((current) => { const next = { ...current }; delete next[id]; return next; });
    try {
      const updated = await setVideoParsePaused(task, !isPaused(task));
      queryClient.setQueryData<VideoParseTaskView[]>(["video-parse-tasks"], (current) =>
        current?.map((item) => item.videoParseTaskId === id ? mergeTaskProjection(item, updated) : item));
    } catch (error) {
      setActionErrors((current) => ({ ...current, [id]: errorMessage(error, locale) }));
    } finally {
      setControllingTaskIds((current) => { const next = new Set(current); next.delete(id); return next; });
      void queryClient.invalidateQueries({ queryKey: ["video-parse-tasks"] });
    }
  };

  const deleteTask = async (task: VideoParseTaskView) => {
    const uploadSessionId = task.uploadSessionId;
    if (
      !uploadSessionId
      || !task.canDelete
      || deletingUploadSessionIds.has(uploadSessionId)
      || retryingUploadSessionIds.has(uploadSessionId)
      || reparsingUploadSessionIds.has(uploadSessionId)
    ) return;
    setConfirmingDeleteTaskId(null);
    setConfirmingReparseTaskId(null);
    setDeletingUploadSessionIds((current) => new Set(current).add(uploadSessionId));
    setActionErrors((current) => {
      const next = { ...current };
      delete next[task.videoParseTaskId];
      return next;
    });
    try {
      await deleteUploadSession(uploadSessionId);
      queryClient.setQueryData<VideoParseTaskView[]>(
        ["video-parse-tasks"],
        (current) => current?.filter((item) => item.uploadSessionId !== uploadSessionId),
      );
      forgetDeletedSession(uploadSessionId);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["video-parse-tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["series"] }),
        queryClient.invalidateQueries({ queryKey: ["series-detail"] }),
        queryClient.invalidateQueries({ queryKey: ["usage-overview"] }),
      ]);
    } catch (error) {
      setActionErrors((current) => ({
        ...current,
        [task.videoParseTaskId]: t("删除失败：{value0}", { value0: errorMessage(error, locale) }),
      }));
    } finally {
      setDeletingUploadSessionIds((current) => {
        const next = new Set(current);
        next.delete(uploadSessionId);
        return next;
      });
    }
  };

  const deleteHistoryVersion = async (task: VideoParseTaskView) => {
    const id = task.videoParseTaskId;
    if (deletingVersionIds.has(id)) return;
    setConfirmingDeleteVersionId(null);
    setDeletingVersionIds((current) => new Set(current).add(id));
    setHistoryErrors((current) => { const next = { ...current }; delete next[`version:${id}`]; return next; });
    try {
      await deleteVideoParseVersion(task);
      queryClient.setQueryData<VideoParseTaskView[]>(["video-parse-tasks"], (current) =>
        current?.filter((item) => item.videoParseTaskId !== id));
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["video-parse-tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["usage-overview"] }),
      ]);
    } catch (error) {
      setHistoryErrors((current) => ({
        ...current,
        [`version:${id}`]: t("删除失败：{value0}", { value0: errorMessage(error, locale) }),
      }));
    } finally {
      setDeletingVersionIds((current) => { const next = new Set(current); next.delete(id); return next; });
    }
  };

  const deleteHistoryForVideo = async (videoId: string) => {
    if (deletingHistoryVideoIds.has(videoId)) return;
    setConfirmingDeleteHistoryVideoId(null);
    setDeletingHistoryVideoIds((current) => new Set(current).add(videoId));
    setHistoryErrors((current) => { const next = { ...current }; delete next[`history:${videoId}`]; return next; });
    try {
      const result = await deleteVideoParseHistory(videoId);
      const deleted = new Set(result.deletedVideoParseTaskIds);
      queryClient.setQueryData<VideoParseTaskView[]>(["video-parse-tasks"], (current) =>
        current?.filter((item) => !deleted.has(item.videoParseTaskId)));
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["video-parse-tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["usage-overview"] }),
      ]);
    } catch (error) {
      setHistoryErrors((current) => ({
        ...current,
        [`history:${videoId}`]: t("删除失败：{value0}", { value0: errorMessage(error, locale) }),
      }));
    } finally {
      setDeletingHistoryVideoIds((current) => { const next = new Set(current); next.delete(videoId); return next; });
    }
  };

  const retryParse = async (task: VideoParseTaskView) => {
    const uploadSessionId = task.uploadSessionId;
    if (
      !uploadSessionId
      || !task.canRetry
      || deletingUploadSessionIds.has(uploadSessionId)
      || retryingUploadSessionIds.has(uploadSessionId)
      || reparsingUploadSessionIds.has(uploadSessionId)
    ) return;
    setConfirmingDeleteTaskId(null);
    setConfirmingReparseTaskId(null);
    setRetryingUploadSessionIds((current) => new Set(current).add(uploadSessionId));
    setActionErrors((current) => {
      const next = { ...current };
      delete next[task.videoParseTaskId];
      return next;
    });
    try {
      const retried = mergeTaskProjection(
        task,
        await retryUploadParse(uploadSessionId, task.version, task.leaseVersion),
      );
      queryClient.setQueryData<VideoParseTaskView[]>(
        ["video-parse-tasks"],
        (current) => current?.map((item) => (
          item.videoParseTaskId === task.videoParseTaskId ? retried : item
        )),
      );
      syncRequeuedParse(retried);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["video-parse-tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["series"] }),
        queryClient.invalidateQueries({ queryKey: ["series-detail"] }),
        queryClient.invalidateQueries({ queryKey: ["usage-overview"] }),
      ]);
    } catch (error) {
      setActionErrors((current) => ({
        ...current,
        [task.videoParseTaskId]: t("重试失败：{value0}", { value0: errorMessage(error, locale) }),
      }));
    } finally {
      setRetryingUploadSessionIds((current) => {
        const next = new Set(current);
        next.delete(uploadSessionId);
        return next;
      });
    }
  };

  const reparse = async (task: VideoParseTaskView) => {
    const uploadSessionId = task.uploadSessionId;
    if (
      !uploadSessionId
      || !task.canReparse
      || deletingUploadSessionIds.has(uploadSessionId)
      || retryingUploadSessionIds.has(uploadSessionId)
      || reparsingUploadSessionIds.has(uploadSessionId)
    ) return;
    setConfirmingDeleteTaskId(null);
    setConfirmingReparseTaskId(null);
    setReparsingUploadSessionIds((current) => new Set(current).add(uploadSessionId));
    setActionErrors((current) => {
      const next = { ...current };
      delete next[task.videoParseTaskId];
      return next;
    });
    try {
      const reparsed = mergeTaskProjection(
        task,
        await reparseUpload(uploadSessionId, task.version),
      );
      queryClient.setQueryData<VideoParseTaskView[]>(
        ["video-parse-tasks"],
        (current) => current?.map((item) => (
          item.videoParseTaskId === task.videoParseTaskId ? reparsed : item
        )),
      );
      syncRequeuedParse(reparsed);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["video-parse-tasks"] }),
        queryClient.invalidateQueries({ queryKey: ["series"] }),
        queryClient.invalidateQueries({ queryKey: ["series-detail"] }),
        queryClient.invalidateQueries({ queryKey: ["usage-overview"] }),
      ]);
    } catch (error) {
      setActionErrors((current) => ({
        ...current,
        [task.videoParseTaskId]: t("{value0}失败：{value1}", { value0: task.status === "PENDING" ? t("开始解析") : t("整集重新解析"), value1: errorMessage(error, locale) }),
      }));
    } finally {
      setReparsingUploadSessionIds((current) => {
        const next = new Set(current);
        next.delete(uploadSessionId);
        return next;
      });
    }
  };

  return (
    <main className="page">
      <header className={styles.header}>
        <div><h1 className="page-title">{t("解析任务")}</h1></div>
        <div className={styles.headerActions}>
          <button className="button-secondary" type="button" onClick={openDialog}>
            <UploadCloud size={15} />{t("管理上传")}
          </button>
          <button className="button-secondary" type="button" disabled={tasksQuery.isFetching} onClick={() => void tasksQuery.refetch()}>
            <RefreshCw size={15} className={tasksQuery.isFetching ? styles.spinning : undefined} />{t("同步状态")}
          </button>
        </div>
      </header>

      <div className={styles.toolbar}>
        <div role="group" aria-label={t("筛选解析任务")}>
          {([
            ["inProgress", t("进行中")],
            ["needsAttention", t("需处理")],
            ["completed", t("已完成")],
            ["all", t("全部")],
          ] as const).map(([value, label]) => (
            <button
              type="button"
              key={value}
              aria-pressed={filter === value}
              aria-label={label}
              className={filter === value ? styles.filterActive : undefined}
              onClick={() => setFilter(value)}
            >
              {label}<span className={styles.filterCount}>{filterCounts[value]}</span>
            </button>
          ))}
        </div>
      </div>

      <section className={styles.jobList} aria-live="polite">
        {tasksQuery.isPending ? (
          <div className={styles.loadState}><RefreshCw className={styles.spinning} size={22} /><p>{t("正在读取解析任务…")}</p></div>
        ) : null}
        {tasksQuery.error instanceof Error ? (
          <div className={styles.loadState} data-error="true"><CircleAlert size={22} /><h2>{t("暂时无法读取任务")}</h2><p>{errorMessage(tasksQuery.error, locale)}</p></div>
        ) : null}
        {visibleTasks.map((task) => {
          const paused = isPaused(task);
          const controlling = controllingTaskIds.has(task.videoParseTaskId);
          const currentStageIndex = PIPELINE_STAGE_INDEX[task.status];
          const progress = task.detail.progress;
          const pipelineProgress = progress
            ? progress.percent ?? 0
            : (currentStageIndex / (PIPELINE.length - 1)) * 100;
          const progressText = progress
            ? progress.percent === null ? "—" : `${Number(progress.percent.toFixed(1))}%`
            : task.status === "COMPLETE" ? "100%" : task.status === "PENDING" ? "0%" : "—";
          const stageTitle = paused ? t("已暂停：进行中的调用会在 1 分钟内停止，已完成的成果保留。")
            : task.activeStage === "asr" ? t("整集语音转写中")
            : activeStatuses.has(task.status)
            ? t("正在{value0}", { value0: t(PIPELINE[currentStageIndex]!) })
            : t(statusText[task.status]);
          const uploadSessionId = task.uploadSessionId ?? null;
          const deleting = uploadSessionId ? deletingUploadSessionIds.has(uploadSessionId) : false;
          const retrying = uploadSessionId ? retryingUploadSessionIds.has(uploadSessionId) : false;
          const reparsing = uploadSessionId ? reparsingUploadSessionIds.has(uploadSessionId) : false;
          const filename = task.originalFilename || task.videoId;
          const episodeOrder = task.episodeOrder ?? 1;
          const pendingTakeover = task.status === "PENDING" && task.canReparse;
          const failed = task.status === "PARSING_FAILED" || task.status === "INDEXING_FAILED";
          const activeOrPaused = activeStatuses.has(task.status) || paused;
          const history = historyByVideo.get(task.videoId) ?? [];
          const historyOpen = expandedHistoryVideoIds.has(task.videoId);
          return (
            <article className={styles.job} key={task.videoParseTaskId}>
              <header className={styles.jobHeader}>
                <div className={styles.episodeSeal}>{episodeLabel(episodeOrder)}</div>
                {/* The episode names the card; the badge carries status, so only live stages repeat as a subline. */}
                <div className={styles.jobName}>
                  <h2>{task.seriesName ? `${task.seriesName} · ` : ""}{episodeTitle(episodeOrder, locale)}</h2>
                  <span className={styles.jobStage}>{t("解析版本 {version}", { version: task.version })}</span>
                  {activeStatuses.has(task.status) ? <span className={styles.jobStage}>{stageTitle}</span> : null}
                </div>
                <div className={styles.jobActions}>
                  <div className={styles.jobStatus} data-status={paused ? "PAUSED" : task.status}>
                    <i />{t(paused ? "解析已暂停" : task.activeStage === "asr" ? "整集语音转写中" : statusText[task.status])}
                  </div>
                  <div className={styles.jobPercent} aria-label={t("解析进度")}>
                    <small>{t("解析进度")}</small>
                    <strong>{progressText}</strong>
                  </div>
                </div>
              </header>

              <div className={styles.jobControls}>
                <span className={styles.uploadState}><CheckCircle2 size={14} />{t("文件已上传")}</span>
                {uploadSessionId ? (
                  <div className={styles.controlButtons}>
                    {task.canPause || task.canResume ? (
                      <button className="button-secondary" type="button"
                        disabled={controlling || deleting || retrying || reparsing}
                        onClick={() => void togglePause(task)}
                        aria-label={`${t(paused ? "继续解析" : "暂停解析")} ${filename}`}>
                        {paused ? <Play size={14} /> : <Pause size={14} />}
                        {controlling ? t("正在更新…") : t(paused ? "继续解析" : "暂停解析")}
                      </button>
                    ) : null}
                    {task.canRetry ? (
                      <button
                        className={clsx("button-primary", styles.retryTask)}
                        type="button"
                        disabled={controlling || deleting || retrying || reparsing}
                        onClick={() => void retryParse(task)}
                        aria-label={t("重试解析 {value0}", { value0: filename })}
                        title={t("重试当前解析任务")}
                      >
                        <RotateCcw size={14} />{retrying ? t("正在重试…") : t("重试解析")}
                      </button>
                    ) : null}
                    {task.canReparse ? (
                      <button
                        className={clsx("button-secondary", styles.reparseTask)}
                        type="button"
                        disabled={controlling || deleting || retrying || reparsing}
                        onClick={() => {
                          setConfirmingDeleteTaskId(null);
                          setConfirmingReparseTaskId(task.videoParseTaskId);
                        }}
                        aria-label={`${pendingTakeover ? t("开始解析") : t("整集重新解析")} ${filename}`}
                        title={pendingTakeover ? t("开始解析这一集") : t("从视频开头重新解析")}
                      >
                        <RefreshCw size={14} />{reparsing ? (pendingTakeover ? t("正在开始解析…") : t("正在重新解析…")) : (pendingTakeover ? t("开始解析") : t("整集重新解析"))}
                      </button>
                    ) : null}
                    <button
                      className={clsx("button-secondary", styles.deleteTask)}
                      type="button"
                      disabled={!task.canDelete || controlling || deleting || retrying || reparsing}
                      onClick={() => {
                        setConfirmingReparseTaskId(null);
                        setConfirmingDeleteTaskId(task.videoParseTaskId);
                      }}
                      aria-label={t("删除视频 {value0}", { value0: filename })}
                      title={t("删除本次上传及其解析产物")}
                    >
                      <Trash2 size={14} />{deleting ? t("正在删除…") : t("删除视频")}
                    </button>
                  </div>
                ) : null}
              </div>

              {confirmingReparseTaskId === task.videoParseTaskId ? (
                <div className={styles.reparseConfirmation} role="alert">
                  <div>
                    <strong>{pendingTakeover ? t("开始解析这一集？") : t("从头重新解析这一整集？")}</strong>
                    <p>{pendingTakeover ? t("将开始解析这集视频，新的模型调用会计入用量。") : t("将从视频开头重新解析，新的模型调用会计入用量。")}</p>
                  </div>
                  <div className={styles.confirmActions}>
                    <button className="button-quiet" type="button" onClick={() => setConfirmingReparseTaskId(null)}>{t("取消")}</button>
                    <button
                      className={clsx("button-primary", styles.confirmReparse)}
                      type="button"
                      disabled={!task.canReparse || deleting || retrying || reparsing}
                      onClick={() => void reparse(task)}
                      aria-label={`${pendingTakeover ? t("确认开始解析") : t("确认整集重新解析")} ${filename}`}
                    >
                      <RefreshCw size={14} />{pendingTakeover ? t("确认开始解析") : t("确认从头解析")}
                    </button>
                  </div>
                </div>
              ) : null}

              {confirmingDeleteTaskId === task.videoParseTaskId ? (
                <div className={styles.deleteConfirmation} role="alert">
                  <div>
                    <strong>{t("确认删除这个视频任务？")}</strong>
                    <p>{activeOrPaused
                      ? t("将先停止解析，再在后台清理上传文件、解析产物和检索索引；已产生的模型费用不会退回。聊天记录会保留，但旧证据无法再播放。此操作无法撤销。")
                      : t("上传文件、解析产物和检索索引都会被删除；聊天记录会保留，但旧证据无法再播放。此操作无法撤销。")}</p>
                  </div>
                  <div className={styles.confirmActions}>
                    <button className="button-quiet" type="button" onClick={() => setConfirmingDeleteTaskId(null)}>{t("取消")}</button>
                    <button
                      className={clsx("button-secondary", styles.confirmDelete)}
                      type="button"
                      disabled={!task.canDelete || controlling || deleting || retrying || reparsing}
                      onClick={() => void deleteTask(task)}
                      aria-label={t("确认删除 {value0}", { value0: filename })}
                    >
                      <Trash2 size={14} />{t("确认删除")}
                    </button>
                  </div>
                </div>
              ) : null}

              {actionErrors[task.videoParseTaskId] ? <p className={styles.actionError} role="alert">{actionErrors[task.videoParseTaskId]}</p> : null}

              <div className={clsx(styles.jobProgress, !isDebug && styles.jobProgressRelease)}>
                <div className={styles.pipeline}>
                  <div className={styles.pipelineLine}><span style={{ width: `${pipelineProgress}%` }} /></div>
                  {PIPELINE.map((stage, index) => (
                    <div
                      key={stage}
                      className={clsx(styles.pipelineStage, index < currentStageIndex && styles.stageDone, index === currentStageIndex && styles.stageCurrent)}
                    >
                      <i /><span>{t(stage)}</span>
                    </div>
                  ))}
                </div>

                {isDebug ? <div className={styles.progressFacts} aria-label={t("解析任务进度")}>
                  <span>{t("固定切片 ")}<strong>{t("{count} 段", { count: task.chunkCount })}</strong></span>
                  {progress ? <>
                    <span>{t("理解 ")}<strong>{progress.completedChunks} / {progress.totalChunks}</strong></span>
                    <span>Refine <strong>{progress.completedRefines} / {progress.totalRefines}</strong></span>
                    <span>{t("索引 ")}<strong>{progress.indexedChunks} / {progress.totalChunks}</strong></span>
                    <span>{t("完成单元 ")}<strong>{progress.completedUnits} / {progress.totalUnits}</strong></span>
                  </> : null}
                </div> : null}
              </div>

              <div className={styles.jobDetail}>
                <dl>
                  <div><dt>{t("文件")}</dt><dd title={filename}>{filename}</dd></div>
                  <div title={t("仅统计实际解析时间，排队和停机不计入")}><dt>{t("有效解析时长")}</dt><dd>{processingDurationText(task, locale)}</dd></div>
                  <div><dt>{t("更新于")}</dt><dd>{new Date(task.updateTime).toLocaleTimeString(formatLocale, { hour: "2-digit", minute: "2-digit" })}</dd></div>
                </dl>
              </div>

              {isDebug ? <ParseUsageDetails runId={task.videoParseTaskId} status={task.status} /> : null}

              {failed ? (
                <div className={styles.jobError}><CircleAlert size={15} /><div><strong>{t("失败原因")}</strong><p>{
                  task.lastError?.message || (task.status === "INDEXING_FAILED"
                    ? t("检索索引发布失败，请重试")
                    : t("视频解析失败，请重试"))
                }</p></div></div>
              ) : null}
              {task.status === "PENDING" ? <p className={styles.retryNote}><Clock3 size={13} />{t("等待开始解析。")}</p> : null}

              {history.length ? (
                <div className={styles.history}>
                  <button
                    type="button"
                    className={styles.historyToggle}
                    aria-expanded={historyOpen}
                    onClick={() => toggleHistory(task.videoId)}
                  >
                    <ChevronDown size={14} />{t("历史版本（{count}）", { count: history.length })}
                  </button>
                  {historyOpen ? (
                    <div className={styles.historyList}>
                      {history.map((version) => {
                        const versionDeleting = deletingVersionIds.has(version.videoParseTaskId);
                        const versionError = historyErrors[`version:${version.videoParseTaskId}`];
                        const versionProgress = version.detail.progress?.percent;
                        return (
                          <div className={styles.historyRow} key={version.videoParseTaskId}>
                            <div className={styles.historyRowMeta}>
                              <span className={styles.historyRowVersion}>{t("版本 {version}", { version: version.version })}</span>
                              <span className={version.isSuperseded ? styles.historyRowStatusSuperseded : styles.historyRowStatus}>
                                {version.isSuperseded ? t("已被新版本替代") : t(statusText[version.status])}
                              </span>
                              <span className={styles.historyRowTime}>{formatCreateTime(version.createTime, formatLocale)}</span>
                              {versionProgress != null ? <span className={styles.historyRowProgress}>{`${Number(versionProgress.toFixed(1))}%`}</span> : null}
                            </div>
                            <button
                              type="button"
                              className={styles.historyDelete}
                              aria-label={t("删除版本 {version}", { version: version.version })}
                              disabled={versionDeleting}
                              onClick={() => setConfirmingDeleteVersionId(version.videoParseTaskId)}
                            >
                              <Trash2 size={14} />
                            </button>
                            {confirmingDeleteVersionId === version.videoParseTaskId ? (
                              <div className={styles.historyConfirm} role="alert">
                                <span>{t("删除这个历史版本？只删除该版本的解析记录和索引，不影响当前版本与检索。")}</span>
                                <div className={styles.historyConfirmActions}>
                                  <button className="button-quiet" type="button" onClick={() => setConfirmingDeleteVersionId(null)}>{t("取消")}</button>
                                  <button className={clsx("button-secondary", styles.confirmDelete)} type="button" onClick={() => void deleteHistoryVersion(version)}>{t("确认删除")}</button>
                                </div>
                              </div>
                            ) : null}
                            {versionError ? <p className={styles.historyError} role="alert">{versionError}</p> : null}
                          </div>
                        );
                      })}
                      <div className={styles.historyBulk}>
                        <button type="button" className={styles.historyDeleteAll} onClick={() => setConfirmingDeleteHistoryVideoId(task.videoId)}>
                          {t("删除全部历史版本")}
                        </button>
                        {confirmingDeleteHistoryVideoId === task.videoId ? (
                          <div className={styles.historyConfirm} role="alert">
                            <span>{t("删除全部历史版本？只删除这些历史版本的解析记录和索引，不影响当前版本与检索。")}</span>
                            <div className={styles.historyConfirmActions}>
                              <button className="button-quiet" type="button" onClick={() => setConfirmingDeleteHistoryVideoId(null)}>{t("取消")}</button>
                              <button className={clsx("button-secondary", styles.confirmDelete)} type="button" onClick={() => void deleteHistoryForVideo(task.videoId)}>{t("确认删除")}</button>
                            </div>
                          </div>
                        ) : null}
                        {historyErrors[`history:${task.videoId}`] ? <p className={styles.historyError} role="alert">{historyErrors[`history:${task.videoId}`]}</p> : null}
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </article>
          );
        })}
        {tasksQuery.isSuccess && visibleTasks.length === 0 ? (
          <div className={styles.empty}>
            {filter === "inProgress" ? (
              <>
                <Activity size={25} strokeWidth={1.4} />
                <h2>{t("没有正在进行的解析")}</h2>
                <button className={clsx("button-primary", styles.emptyAction)} type="button" onClick={() => setFilter("all")}>{t("查看全部")}</button>
              </>
            ) : (
              <>
                <Activity size={25} strokeWidth={1.4} />
                <h2>{t("这里暂时很安静")}</h2>
                <p>{tasks.length === 0 ? t("上传视频后，可以在这里查看解析进度。") : t("没有符合当前筛选条件的任务。")}</p>
              </>
            )}
          </div>
        ) : null}
      </section>
    </main>
  );
}
