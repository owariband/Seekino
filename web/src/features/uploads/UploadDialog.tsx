import clsx from "clsx";
import {
  CheckCircle2,
  FileVideo2,
  Pause,
  Play,
  RotateCcw,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { type ChangeEvent, type DragEvent, useEffect, useRef, useState } from "react";

import { useWorkspace } from "../../app/workspace";
import { formatBytes, formatTimecode, formatTimeRange, inferEpisodeOrder, publicParseStageLabel } from "../../shared/format";
import { useUploads } from "./UploadProvider";
import type { UploadInput, UploadTask } from "./upload-types";
import { mediaTypeForFile, taskProgress, taskStatusLabel } from "./upload-types";
import styles from "./upload-dialog.module.css";

function TaskRow({ task }: { task: UploadTask }) {
  const { pause, resume, attachFile, retry, cancel } = useUploads();
  const replacementRef = useRef<HTMLInputElement>(null);
  const progress = taskProgress(task);
  const canPause = ["queued", "creating", "uploading"].includes(task.status);
  const canResume = task.status === "paused" && Boolean(task.file);
  const needsOriginalFile = task.status === "paused" && !task.file;
  const canResumeCompletion = !task.file
    && task.session?.status === "COMPLETING"
    && ["completing", "failed"].includes(task.status);
  const parsePosition = task.job?.currentChunk
    ? `当前解析 ${formatTimeRange(task.job.currentChunk.startMs, task.job.currentChunk.endMs)}`
    : task.job?.completedThroughMs !== null && task.job?.completedThroughMs !== undefined
      ? `连续解析到 ${formatTimecode(task.job.completedThroughMs)} / ${formatTimecode(task.job.totalDurationMs)}`
      : "等待精确检查点";

  return (
    <article className={styles.task} data-status={task.status}>
      <div className={styles.taskIndex}>{String(task.episodeOrder).padStart(2, "0")}</div>
      <div className={styles.taskBody}>
        <div className={styles.taskHeading}>
          <div>
            <strong>{task.fileName}</strong>
            <span>
              {formatBytes(task.totalBytes)} · {taskStatusLabel(task)}
            </span>
          </div>
          <b>{progress}%</b>
        </div>
        <div
          className={styles.progressTrack}
          role="progressbar"
          aria-label={`${task.fileName} 上传进度`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress}
        >
          <span style={{ width: `${progress}%` }} />
        </div>
        {task.status === "processing" && task.job ? (
          <div className={styles.stageLine}>
            <span>{publicParseStageLabel(task.job.stage)} · {parsePosition}</span>
            <span>{task.job.attemptCount}/{task.job.maxAttempts} 次尝试</span>
          </div>
        ) : null}
        {task.error ? <p className={styles.error}>{task.error}</p> : null}
      </div>
      <div className={styles.taskActions}>
        {canPause ? (
          <button type="button" onClick={() => pause(task.taskId)} aria-label={`暂停 ${task.fileName}`}>
            <Pause size={16} />
          </button>
        ) : null}
        {canResume ? (
          <button type="button" onClick={() => resume(task.taskId)} aria-label={`继续 ${task.fileName}`}>
            <Play size={16} />
          </button>
        ) : null}
        {needsOriginalFile ? <>
          <input
            ref={replacementRef}
            type="file"
            accept="video/mp4,video/quicktime,video/x-msvideo,.mp4,.mov,.avi"
            className={styles.hiddenFileInput}
            aria-label={`选择原文件继续 ${task.fileName}`}
            onChange={(event) => {
              const selected = event.target.files?.[0];
              if (selected) attachFile(task.taskId, selected);
              event.target.value = "";
            }}
          />
          <button type="button" onClick={() => replacementRef.current?.click()} aria-label={`重新选择原文件继续 ${task.fileName}`} title="重新选择同一个原文件，从已确认分片继续">
            <Play size={16} />
          </button>
        </> : null}
        {canResumeCompletion ? (
          <button type="button" onClick={() => resume(task.taskId)} aria-label={`继续合并 ${task.fileName}`} title="重新查询并继续服务端合并">
            <RotateCcw size={16} />
          </button>
        ) : null}
        {task.status === "failed" && task.file ? (
          <button type="button" onClick={() => retry(task.taskId)} aria-label={`重试 ${task.fileName}`}>
            <RotateCcw size={16} />
          </button>
        ) : null}
        {task.status === "completed" ? <CheckCircle2 size={18} aria-label="完成" /> : null}
        {task.session?.status !== "COMPLETED" && task.session?.status !== "COMPLETING" && !["processing", "completed", "completing", "cancelled"].includes(task.status) ? (
          <button type="button" disabled={task.status === "deleting"} onClick={() => void cancel(task.taskId)} aria-label={`删除 ${task.fileName}`} title="终止上传并删除已上传的分片">
            <Trash2 size={16} />
          </button>
        ) : null}
      </div>
    </article>
  );
}

export function UploadDialog() {
  const {
    dialogOpen,
    closeDialog,
    enqueue,
    visibleTasks,
    activeCount,
    overallProgress,
    dismissFinished,
  } = useUploads();
  const { mode, selectedSeries } = useWorkspace();
  const [pending, setPending] = useState<UploadInput[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!dialogOpen) setDragging(false);
  }, [dialogOpen]);

  if (!dialogOpen) return null;

  const addFiles = (list: FileList | File[]) => {
    const files = Array.from(list).filter((file) => mediaTypeForFile(file) !== null);
    const base = Math.max(
      0,
      ...(selectedSeries?.videos.map((video) => video.episodeOrder) ?? []),
      ...pending.map((item) => item.episodeOrder),
    );
    setPending((current) => [
      ...current,
      ...files.map((file, index) => ({
        file,
        episodeOrder: inferEpisodeOrder(file.name, base + index + 1),
      })),
    ]);
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    addFiles(event.dataTransfer.files);
  };

  const onInput = (event: ChangeEvent<HTMLInputElement>) => {
    if (event.target.files) addFiles(event.target.files);
    event.target.value = "";
  };

  const start = () => {
    enqueue(pending);
    setPending([]);
  };

  return (
    <div className={styles.backdrop} role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) closeDialog();
    }}>
      <section className={styles.dialog} role="dialog" aria-modal="true" aria-labelledby="upload-title">
        <header className={styles.header}>
          <div>
            <p className="page-kicker">Transfer desk · 传输案台</p>
            <h2 id="upload-title">把每一集，安稳放进资料库</h2>
          </div>
          <button className={styles.close} type="button" onClick={closeDialog} aria-label="关闭上传面板">
            <X size={20} />
          </button>
        </header>

        <div className={styles.summary}>
          <div>
            <span className="eyebrow">本批次</span>
            <strong>{activeCount > 0 ? `${activeCount} 个视频处理中` : "等待新视频"}</strong>
          </div>
          <div className={styles.summaryProgress}>
            <span style={{ width: `${overallProgress}%` }} />
          </div>
          <b>{overallProgress}%</b>
        </div>

        <div
          className={clsx(styles.dropzone, dragging && styles.dropzoneActive)}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false);
          }}
          onDrop={onDrop}
        >
          <UploadCloud size={25} strokeWidth={1.5} />
          <div>
            <strong>拖入 2–3 个视频，也可以更多</strong>
            <span>最多 3 个文件并行；每个文件保留独立进度与断点</span>
          </div>
          <button className="button-secondary" type="button" onClick={() => inputRef.current?.click()}>
            选择视频
          </button>
          <input ref={inputRef} className="sr-only" type="file" accept=".mp4,.mov,.avi,video/mp4,video/quicktime,video/x-msvideo" multiple onChange={onInput} />
        </div>

        {pending.length > 0 ? (
          <section className={styles.pending} aria-label="待上传文件">
            <div className={styles.sectionTitle}>
              <span>{pending.length} 个文件待确认</span>
              <small>集数可在开始前修正</small>
            </div>
            {pending.map((item, index) => (
              <div className={styles.pendingRow} key={`${item.file.name}-${item.file.lastModified}`}>
                <FileVideo2 size={18} />
                <div>
                  <strong>{item.file.name}</strong>
                  <span>{formatBytes(item.file.size)}</span>
                </div>
                <label>
                  <span>EP</span>
                  <input
                    aria-label={`${item.file.name} 集数`}
                    type="number"
                    min={1}
                    value={item.episodeOrder}
                    onChange={(event) =>
                      setPending((current) =>
                        current.map((entry, entryIndex) =>
                          entryIndex === index
                            ? { ...entry, episodeOrder: Math.max(1, Number(event.target.value)) }
                            : entry,
                        ),
                      )
                    }
                  />
                </label>
                <button
                  className={styles.iconButton}
                  type="button"
                  onClick={() => setPending((current) => current.filter((_, entryIndex) => entryIndex !== index))}
                  aria-label={`移除 ${item.file.name}`}
                >
                  <X size={16} />
                </button>
              </div>
            ))}
            <div className={styles.pendingActions}>
              <span>上传到「{selectedSeries?.name ?? "未选择系列"}」</span>
              <button className="button-primary" type="button" disabled={!selectedSeries || mode !== "live"} onClick={start}>
                开始并行上传
              </button>
            </div>
          </section>
        ) : null}

        <section className={styles.queue} aria-label="上传队列">
          <div className={styles.sectionTitle}>
            <span>并行队列</span>
            {visibleTasks.some((task) => task.status === "completed") ? (
              <button type="button" onClick={dismissFinished}>收起已完成</button>
            ) : (
              <small>每条进度互不遮挡</small>
            )}
          </div>
          {visibleTasks.length > 0 ? (
            <div className={styles.taskList}>
              {visibleTasks.map((task) => <TaskRow key={task.taskId} task={task} />)}
            </div>
          ) : (
            <p className={styles.empty}>还没有传输记录。把视频拖到上方即可开始。</p>
          )}
        </section>

        <footer className={styles.footer}>
          <span><i /> 分片校验后才计入已上传；暂停后可从服务端确认点恢复。</span>
          <mark>{mode === "live" ? "LIVE API" : "API OFFLINE"}</mark>
        </footer>
      </section>
    </div>
  );
}
