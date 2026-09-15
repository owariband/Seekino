import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Activity,
  CheckCircle2,
  CircleAlert,
  Clock3,
  RefreshCw,
  Rows3,
} from "lucide-react";
import { useState } from "react";

import { listAllJobs, shouldRetryApiRequest } from "../../api/client";
import type { ParseJobStatus } from "../../api/contracts";
import { episodeLabel, stageLabel } from "../../shared/format";
import styles from "./tasks.module.css";
import { ParseUsageDetails } from "./ParseUsageDetails";

type Filter = "all" | "active" | "failed" | "completed";

const PIPELINE = [
  "QUEUED",
  "PREPARING",
  "SPLIT_READY",
  "PARSING",
  "TEXT_EMBEDDING",
  "VISION_EMBEDDING",
  "PERSISTING",
  "INDEXING",
  "COMPLETED",
];

const statusText: Record<ParseJobStatus, string> = {
  QUEUED: "等待",
  RUNNING: "进行中",
  RETRY_WAIT: "等待重试",
  SUCCEEDED: "已完成",
  FAILED: "失败",
};

export function TasksPage() {
  const [filter, setFilter] = useState<Filter>("all");
  const jobsQuery = useQuery({
    queryKey: ["jobs"],
    queryFn: ({ signal }) => listAllJobs(signal),
    retry: shouldRetryApiRequest,
    refetchInterval: 5_000,
  });

  const jobs = jobsQuery.data ?? [];
  const visibleJobs = jobs.filter((job) => {
    if (filter === "active") return ["QUEUED", "RUNNING", "RETRY_WAIT"].includes(job.status);
    if (filter === "failed") return job.status === "FAILED";
    if (filter === "completed") return job.status === "SUCCEEDED";
    return true;
  });
  const activeCount = jobs.filter((job) => ["QUEUED", "RUNNING", "RETRY_WAIT"].includes(job.status)).length;
  const completedCount = jobs.filter((job) => job.status === "SUCCEEDED").length;
  const checkpointCount = jobs.reduce((sum, job) => sum + job.checkpoints.length, 0);

  return (
    <main className="page">
      <header className={styles.header}>
        <div>
          <h1 className="page-title">解析任务</h1>
        </div>
        <button className="button-secondary" type="button" disabled={jobsQuery.isFetching} onClick={() => void jobsQuery.refetch()}>
          <RefreshCw size={15} className={jobsQuery.isFetching ? styles.spinning : undefined} />同步状态
        </button>
      </header>

      <section className={styles.metrics} aria-label="任务概况">
        <article><Activity size={17} /><div><span>处理中</span><strong>{activeCount}</strong></div></article>
        <article><Rows3 size={17} /><div><span>切片检查点</span><strong>{checkpointCount}</strong></div></article>
        <article><CheckCircle2 size={17} /><div><span>已完成</span><strong>{completedCount}</strong></div></article>
      </section>

      <div className={styles.toolbar}>
        <div role="group" aria-label="筛选解析任务">
          {([
            ["all", "全部"],
            ["active", "进行中"],
            ["failed", "异常"],
            ["completed", "已完成"],
          ] as const).map(([value, label]) => (
            <button type="button" key={value} className={filter === value ? styles.filterActive : undefined} onClick={() => setFilter(value)}>{label}</button>
          ))}
        </div>
        <span>SSE 实时更新 · 断线后以服务端快照为准</span>
      </div>

      <section className={styles.jobList} aria-live="polite">
        {jobsQuery.isPending ? (
          <div className={styles.loadState}><RefreshCw className={styles.spinning} size={22} /><p>正在从 API 读取解析任务…</p></div>
        ) : null}
        {jobsQuery.error instanceof Error ? (
          <div className={styles.loadState} data-error="true"><CircleAlert size={22} /><h2>任务接口暂时不可用</h2><p>{jobsQuery.error.message}</p></div>
        ) : null}
        {visibleJobs.map((job) => {
          const currentStageIndex = Math.max(0, PIPELINE.indexOf(job.stage));
          return (
            <article className={styles.job} key={job.jobId}>
              <header className={styles.jobHeader}>
                <div className={styles.episodeSeal}>{episodeLabel(job.episodeOrder)}</div>
                <div className={styles.jobName}>
                  <span className="eyebrow">{job.seriesName}</span>
                  <h2>{stageLabel(job.stage)}</h2>
                </div>
                <div className={styles.jobStatus} data-status={job.status}>
                  <i />{statusText[job.status]}
                </div>
                <strong className={styles.jobPercent}>{job.progress}%</strong>
              </header>

              <div className={styles.pipeline}>
                <div className={styles.pipelineLine}><span style={{ width: `${job.progress}%` }} /></div>
                {PIPELINE.map((stage, index) => (
                  <div
                    key={stage}
                    className={clsx(styles.pipelineStage, index < currentStageIndex && styles.stageDone, index === currentStageIndex && styles.stageCurrent)}
                  >
                    <i />
                    <span>{stageLabel(stage)}</span>
                  </div>
                ))}
              </div>

              <div className={styles.jobDetail}>
                <div className={styles.checkpoints}>
                  <div className={styles.detailTitle}><span>切片检查点 · {job.checkpoints.length}</span><small>颜色越深，处理越靠后</small></div>
                  <div className={styles.checkpointGrid}>
                    {job.checkpoints.length > 0 ? job.checkpoints.map((checkpoint) => (
                      <i key={checkpoint.videoChunkId} data-stage={checkpoint.stage} title={`片段 ${checkpoint.sequenceNo + 1} · ${checkpoint.stage}`} />
                    )) : <span>等待媒体切片完成后生成</span>}
                  </div>
                </div>
                <dl>
                  <div><dt>文件</dt><dd>{job.originalFilename}</dd></div>
                  <div><dt>尝试次数</dt><dd>{job.attemptCount} / {job.maxAttempts}</dd></div>
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
