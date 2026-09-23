import { useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Activity,
  CheckCircle2,
  CircleAlert,
  Clock3,
  RefreshCw,
  RotateCcw,
  Rows3,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { useState } from "react";

import {
  deleteUploadSession,
  errorMessage,
  listAllJobs,
  reparseUpload,
  retryUploadParse,
  shouldRetryApiRequest,
} from "../../api/client";
import type { ParseJobStatus, ParseJobView } from "../../api/contracts";
import { useUploads } from "../uploads/UploadProvider";
import { episodeLabel, formatTimecode, formatTimeRange } from "../../shared/format";
import styles from "./tasks.module.css";
import { ParseUsageDetails } from "./ParseUsageDetails";

type Filter = "all" | ParseJobStatus;

const PIPELINE = ["解析画面与对白", "发布检索结果", "可检索"];

const PIPELINE_STAGE_INDEX: Record<string, number> = {
  QUEUED: 0,
  PREPARING: 0,
  SPLIT_READY: 0,
  PARSING: 0,
  TEXT_EMBEDDING: 1,
  VISION_EMBEDDING: 1,
  PERSISTING: 1,
  INDEXING: 1,
  COMPLETED: 2,
};

const statusText: Record<ParseJobStatus, string> = {
  QUEUED: "排队中",
  RUNNING: "进行中",
  RETRY_WAIT: "等待重试",
  SUCCEEDED: "已完成",
  FAILED: "异常",
};

function formatProcessingDuration(durationMs: number): string {
  if (durationMs < 1_000) return `${durationMs} 毫秒`;
  if (durationMs < 60_000) return `${(durationMs / 1_000).toFixed(durationMs % 1_000 === 0 ? 0 : 1)} 秒`;
  const totalSeconds = Math.floor(durationMs / 1_000);
  return `${Math.floor(totalSeconds / 60)} 分 ${totalSeconds % 60} 秒`;
}

function processingDurationText(job: ParseJobView): string {
  if (job.attemptCount === 0) return "未开始";
  if (job.status === "RUNNING") {
    return job.processingDurationMs > 0
      ? `${formatProcessingDuration(job.processingDurationMs)}（进行中）`
      : "首个片段处理中";
  }
  if (job.processingTimingComplete) return formatProcessingDuration(job.processingDurationMs);
  return job.processingDurationMs > 0
    ? `至少 ${formatProcessingDuration(job.processingDurationMs)}（不完整）`
    : "尚未完整记录";
}

export function TasksPage() {
  const { forgetDeletedSession, openDialog, syncRequeuedParse } = useUploads();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState<Filter>("all");
  const [deletingUploadSessionIds, setDeletingUploadSessionIds] = useState<ReadonlySet<string>>(() => new Set());
  const [retryingUploadSessionIds, setRetryingUploadSessionIds] = useState<ReadonlySet<string>>(() => new Set());
  const [reparsingUploadSessionIds, setReparsingUploadSessionIds] = useState<ReadonlySet<string>>(() => new Set());
  const [confirmingDeleteJobId, setConfirmingDeleteJobId] = useState<string | null>(null);
  const [confirmingReparseJobId, setConfirmingReparseJobId] = useState<string | null>(null);
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: ({ signal }) => listAllJobs(signal),
    retry: shouldRetryApiRequest,
    refetchInterval: 5_000,
  });

  const deleteTask = async (job: ParseJobView) => {
    const uploadSessionId = job.uploadSessionId;
    if (!uploadSessionId || job.status === "RUNNING" || deletingUploadSessionIds.has(uploadSessionId) || retryingUploadSessionIds.has(uploadSessionId) || reparsingUploadSessionIds.has(uploadSessionId)) return;
    setConfirmingDeleteJobId(null);
    setConfirmingReparseJobId(null);
    setDeletingUploadSessionIds((current) => new Set(current).add(uploadSessionId));
    setActionErrors((current) => {
      const next = { ...current };
      delete next[job.jobId];
      return next;
    });
    try {
      await deleteUploadSession(uploadSessionId);
      queryClient.setQueryData<ParseJobView[]>(["jobs"], (current) =>
        current?.filter((item) => item.uploadSessionId !== uploadSessionId),
      );
      forgetDeletedSession(uploadSessionId);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["series"] }),
        queryClient.invalidateQueries({ queryKey: ["series-detail"] }),
        queryClient.invalidateQueries({ queryKey: ["usage-overview"] }),
      ]);
    } catch (error) {
      setActionErrors((current) => ({
        ...current,
        [job.jobId]: `删除失败：${errorMessage(error)}`,
      }));
    } finally {
      setDeletingUploadSessionIds((current) => {
        const next = new Set(current);
        next.delete(uploadSessionId);
        return next;
      });
    }
  };

  const retryParse = async (job: ParseJobView) => {
    const uploadSessionId = job.uploadSessionId;
    if (!uploadSessionId || !job.canRetry || deletingUploadSessionIds.has(uploadSessionId) || retryingUploadSessionIds.has(uploadSessionId) || reparsingUploadSessionIds.has(uploadSessionId)) return;
    setConfirmingDeleteJobId(null);
    setConfirmingReparseJobId(null);
    setRetryingUploadSessionIds((current) => new Set(current).add(uploadSessionId));
    setActionErrors((current) => {
      const next = { ...current };
      delete next[job.jobId];
      return next;
    });
    try {
      const retried = await retryUploadParse(uploadSessionId, job.version);
      queryClient.setQueryData<ParseJobView[]>(["jobs"], (current) =>
        current?.map((item) => item.jobId === job.jobId ? retried : item),
      );
      syncRequeuedParse(retried);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["series"] }),
        queryClient.invalidateQueries({ queryKey: ["series-detail"] }),
        queryClient.invalidateQueries({ queryKey: ["parse-usage"] }),
        queryClient.invalidateQueries({ queryKey: ["usage-overview"] }),
      ]);
    } catch (error) {
      setActionErrors((current) => ({
        ...current,
        [job.jobId]: `重试失败：${errorMessage(error)}`,
      }));
    } finally {
      setRetryingUploadSessionIds((current) => {
        const next = new Set(current);
        next.delete(uploadSessionId);
        return next;
      });
    }
  };

  const reparse = async (job: ParseJobView) => {
    const uploadSessionId = job.uploadSessionId;
    if (!uploadSessionId || !job.canReparse || deletingUploadSessionIds.has(uploadSessionId) || retryingUploadSessionIds.has(uploadSessionId) || reparsingUploadSessionIds.has(uploadSessionId)) return;
    setConfirmingDeleteJobId(null);
    setConfirmingReparseJobId(null);
    setReparsingUploadSessionIds((current) => new Set(current).add(uploadSessionId));
    setActionErrors((current) => {
      const next = { ...current };
      delete next[job.jobId];
      return next;
    });
    try {
      const reparsed = await reparseUpload(uploadSessionId, job.version);
      queryClient.setQueryData<ParseJobView[]>(["jobs"], (current) =>
        current?.map((item) => item.jobId === job.jobId ? reparsed : item),
      );
      syncRequeuedParse(reparsed);
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ["jobs"] }),
        queryClient.invalidateQueries({ queryKey: ["series"] }),
        queryClient.invalidateQueries({ queryKey: ["series-detail"] }),
        queryClient.invalidateQueries({ queryKey: ["parse-usage"] }),
        queryClient.invalidateQueries({ queryKey: ["usage-overview"] }),
      ]);
    } catch (error) {
      setActionErrors((current) => ({
        ...current,
        [job.jobId]: `整集重新解析失败：${errorMessage(error)}`,
      }));
    } finally {
      setReparsingUploadSessionIds((current) => {
        const next = new Set(current);
        next.delete(uploadSessionId);
        return next;
      });
    }
  };

  const jobs = jobsQuery.data ?? [];
  const visibleJobs = filter === "all" ? jobs : jobs.filter((job) => job.status === filter);
  const runningCount = jobs.filter((job) => job.status === "RUNNING").length;
  const completedCount = jobs.filter((job) => job.status === "SUCCEEDED").length;
  const checkpointCount = jobs.reduce((sum, job) => sum + job.checkpoints.length, 0);

  return (
    <main className="page">
      <header className={styles.header}>
        <div>
          <h1 className="page-title">解析任务</h1>
        </div>
        <div className={styles.headerActions}>
          <button className="button-secondary" type="button" onClick={openDialog}><UploadCloud size={15} />管理上传</button>
          <button className="button-secondary" type="button" disabled={jobsQuery.isFetching} onClick={() => void jobsQuery.refetch()}>
            <RefreshCw size={15} className={jobsQuery.isFetching ? styles.spinning : undefined} />同步状态
          </button>
        </div>
      </header>

      <section className={styles.metrics} aria-label="任务概况">
        <article><Activity size={17} /><div><span>进行中</span><strong>{runningCount}</strong></div></article>
        <article><Rows3 size={17} /><div><span>切片检查点</span><strong>{checkpointCount}</strong></div></article>
        <article><CheckCircle2 size={17} /><div><span>已完成</span><strong>{completedCount}</strong></div></article>
      </section>

      <div className={styles.toolbar}>
        <div role="group" aria-label="筛选解析任务">
          {([
            ["all", "全部"],
            ["QUEUED", "排队中"],
            ["RUNNING", "进行中"],
            ["RETRY_WAIT", "等待重试"],
            ["FAILED", "异常"],
            ["SUCCEEDED", "已完成"],
          ] as const).map(([value, label]) => (
            <button type="button" key={value} aria-pressed={filter === value} className={filter === value ? styles.filterActive : undefined} onClick={() => setFilter(value)}>{label}</button>
          ))}
        </div>
        <span>每 5 秒自动同步 · 手动刷新可立即校准</span>
      </div>

      <section className={styles.jobList} aria-live="polite">
        {jobsQuery.isPending ? (
          <div className={styles.loadState}><RefreshCw className={styles.spinning} size={22} /><p>正在从 API 读取解析任务…</p></div>
        ) : null}
        {jobsQuery.error instanceof Error ? (
          <div className={styles.loadState} data-error="true"><CircleAlert size={22} /><h2>任务接口暂时不可用</h2><p>{jobsQuery.error.message}</p></div>
        ) : null}
        {visibleJobs.map((job) => {
          const currentStageIndex = PIPELINE_STAGE_INDEX[job.stage] ?? 0;
          const pipelineProgress = (currentStageIndex / (PIPELINE.length - 1)) * 100;
          const userStageLabel = PIPELINE[currentStageIndex];
          const jobStageTitle = job.status === "RUNNING" && job.stage !== "COMPLETED"
            ? `正在${userStageLabel}`
            : userStageLabel;
          const deleting = job.uploadSessionId ? deletingUploadSessionIds.has(job.uploadSessionId) : false;
          const retrying = job.uploadSessionId ? retryingUploadSessionIds.has(job.uploadSessionId) : false;
          const reparsing = job.uploadSessionId ? reparsingUploadSessionIds.has(job.uploadSessionId) : false;
          const running = job.status === "RUNNING";
          return (
            <article className={styles.job} key={job.jobId}>
              <header className={styles.jobHeader}>
                <div className={styles.episodeSeal}>{episodeLabel(job.episodeOrder)}</div>
                <div className={styles.jobName}>
                  <span className="eyebrow">{job.seriesName}</span>
                  <h2>{jobStageTitle}</h2>
                </div>
                <div className={styles.jobActions}>
                  <div className={styles.jobStatus} data-status={job.status}>
                    <i />{statusText[job.status]}
                  </div>
                  <div className={styles.jobPercent}>
                    <small>任务进度</small>
                    <strong>{job.overallProgress === null ? "—" : `${job.overallProgress}%`}</strong>
                  </div>
                </div>
              </header>

              <div className={styles.jobControls}>
                <span className={styles.uploadState}><CheckCircle2 size={14} />文件已上传</span>
                {job.uploadSessionId ? (
                  <div className={styles.controlButtons}>
                    {job.canRetry ? (
                      <button
                        className={clsx("button-primary", styles.retryTask)}
                        type="button"
                        disabled={deleting || retrying || reparsing}
                        onClick={() => void retryParse(job)}
                        aria-label={`重试解析 ${job.originalFilename}`}
                        title="从已有检查点继续解析"
                      >
                        <RotateCcw size={14} />{retrying ? "正在重试…" : "重试解析"}
                      </button>
                    ) : null}
                    {job.canReparse ? (
                      <button
                        className={clsx("button-secondary", styles.reparseTask)}
                        type="button"
                        disabled={deleting || retrying || reparsing}
                        onClick={() => {
                          setConfirmingDeleteJobId(null);
                          setConfirmingReparseJobId(job.jobId);
                        }}
                        aria-label={`整集重新解析 ${job.originalFilename}`}
                        title="清空本集解析检查点，从视频开头重新解析"
                      >
                        <RefreshCw size={14} />{reparsing ? "正在重新解析…" : "整集重新解析"}
                      </button>
                    ) : null}
                    <button
                      className={clsx("button-secondary", styles.deleteTask)}
                      type="button"
                      disabled={running || deleting || retrying || reparsing}
                      onClick={() => {
                        setConfirmingReparseJobId(null);
                        setConfirmingDeleteJobId(job.jobId);
                      }}
                      aria-label={`删除任务 ${job.originalFilename}`}
                      title={running ? "任务正在运行，等待本次执行结束后再删除" : "删除本次上传及其解析产物"}
                    >
                      <Trash2 size={14} />{deleting ? "正在删除…" : "删除任务"}
                    </button>
                  </div>
                ) : null}
              </div>

              {confirmingReparseJobId === job.jobId ? (
                <div className={styles.reparseConfirmation} role="alert">
                  <div>
                    <strong>从头重新解析这一整集？</strong>
                    <p>本任务的切片检查点会被清空，并从视频开头重新运行。当前可检索结果会保留到新结果导入成功；新的模型调用会计入用量。</p>
                  </div>
                  <div className={styles.confirmActions}>
                    <button className="button-quiet" type="button" onClick={() => setConfirmingReparseJobId(null)}>取消</button>
                    <button
                      className={clsx("button-primary", styles.confirmReparse)}
                      type="button"
                      disabled={running || deleting || retrying || reparsing}
                      onClick={() => void reparse(job)}
                      aria-label={`确认整集重新解析 ${job.originalFilename}`}
                    >
                      <RefreshCw size={14} />确认从头解析
                    </button>
                  </div>
                </div>
              ) : null}

              {confirmingDeleteJobId === job.jobId ? (
                <div className={styles.deleteConfirmation} role="alert">
                  <div>
                    <strong>确认删除这个视频任务？</strong>
                    <p>上传文件、解析产物和检索索引都会被删除；聊天记录会保留，但旧证据无法再播放。此操作无法撤销。</p>
                  </div>
                  <div className={styles.confirmActions}>
                    <button className="button-quiet" type="button" onClick={() => setConfirmingDeleteJobId(null)}>取消</button>
                    <button
                      className={clsx("button-secondary", styles.confirmDelete)}
                      type="button"
                      disabled={running || deleting || retrying || reparsing}
                      onClick={() => void deleteTask(job)}
                      aria-label={`确认删除 ${job.originalFilename}`}
                    >
                      <Trash2 size={14} />确认删除
                    </button>
                  </div>
                </div>
              ) : null}

              {actionErrors[job.jobId] ? <p className={styles.actionError} role="alert">{actionErrors[job.jobId]}</p> : null}

              <div className={styles.pipeline}>
                <div className={styles.pipelineLine}><span style={{ width: `${pipelineProgress}%` }} /></div>
                {PIPELINE.map((stage, index) => (
                  <div
                    key={stage}
                    className={clsx(styles.pipelineStage, index < currentStageIndex && styles.stageDone, index === currentStageIndex && styles.stageCurrent)}
                  >
                    <i />
                    <span>{stage}</span>
                  </div>
                ))}
              </div>

              <div className={styles.progressFacts} aria-label="解析任务进度">
                <span>
                  连续解析到{" "}
                  <strong>
                    {job.completedThroughSeconds === null
                      ? "等待检查点"
                      : `${formatTimecode(job.completedThroughSeconds)} / ${formatTimecode(job.videoDurationSeconds)}`}
                  </strong>
                </span>
                <span>
                  已解析{" "}
                  <strong>
                    {job.completedChunkCount === null
                      ? `— / ${job.totalChunkCount} 段`
                      : `${job.completedChunkCount} / ${job.totalChunkCount} 段`}
                  </strong>
                </span>
                {job.currentChunk ? (
                  <span>
                    当前解析 <strong>{formatTimeRange(job.currentChunk.startSeconds, job.currentChunk.endSeconds)}</strong>
                  </span>
                ) : null}
              </div>

              <div className={styles.jobDetail}>
                <div className={styles.checkpoints}>
                  <div className={styles.detailTitle}><span>切片检查点 · {job.checkpoints.length}</span><small>颜色越深，处理越靠后</small></div>
                  <div className={styles.checkpointGrid}>
                    {job.checkpoints.length > 0 ? job.checkpoints.map((checkpoint) => (
                      <i
                        key={checkpoint.videoChunkId}
                        data-stage={checkpoint.stage}
                        title={checkpoint.chunkStartSeconds === null || checkpoint.chunkEndSeconds === null
                          ? `片段 ${checkpoint.sequenceNo + 1} · ${checkpoint.stage}`
                          : `片段 ${checkpoint.sequenceNo + 1} · ${formatTimeRange(checkpoint.chunkStartSeconds, checkpoint.chunkEndSeconds)} · ${checkpoint.stage}`}
                      />
                    )) : <span>等待媒体切片完成后生成</span>}
                  </div>
                </div>
                <dl>
                  <div><dt>文件</dt><dd>{job.originalFilename}</dd></div>
                  <div><dt>尝试次数</dt><dd>{job.attemptCount} / {job.maxAttempts}</dd></div>
                  <div><dt>{job.status === "SUCCEEDED" || job.status === "FAILED" ? "解析耗时" : "累计解析耗时"}</dt><dd>{processingDurationText(job)}</dd></div>
                  <div><dt>流水线</dt><dd>{job.pipelineVersion}</dd></div>
                  <div><dt>更新于</dt><dd>{new Date(job.updatedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</dd></div>
                </dl>
              </div>

              {job.lastError ? (
                <div className={styles.jobError}><CircleAlert size={15} /><div><strong>最近一次错误</strong><p>{job.lastError.message}</p></div></div>
              ) : null}
              {job.status === "RETRY_WAIT" ? <p className={styles.retryNote}><Clock3 size={13} />服务端会在可用时间自动重试，前端不会重复创建任务。</p> : null}
              <ParseUsageDetails jobId={job.jobId} status={job.status} />
            </article>
          );
        })}
        {jobsQuery.isSuccess && visibleJobs.length === 0 ? (
          <div className={styles.empty}><Activity size={25} strokeWidth={1.4} /><h2>这里暂时很安静</h2><p>上传完成并创建解析任务后，阶段与切片检查点会出现在这里。</p></div>
        ) : null}
      </section>
    </main>
  );
}
