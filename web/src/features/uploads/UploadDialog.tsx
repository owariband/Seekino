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

import { errorMessage, getParseConfig } from "../../api/client";
import type { ParseConfig } from "../../api/contracts";
import { useWorkspace } from "../../app/workspace";
import { formatBytes, formatTimecode, formatTimeRange, inferEpisodeOrder, publicParseStageLabel } from "../../shared/format";
import { useUploads } from "./UploadProvider";
import type { UploadInput, UploadTask } from "./upload-types";
import { configuredMax, configuredMin, mediaTypeForFile, taskProgress, taskStatusLabel, validateParseConfig } from "./upload-types";
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
    ? `当前解析 ${formatTimeRange(task.job.currentChunk.startSeconds, task.job.currentChunk.endSeconds)}`
    : task.job?.completedThroughSeconds !== null && task.job?.completedThroughSeconds !== undefined
      ? `连续解析到 ${formatTimecode(task.job.completedThroughSeconds)} / ${formatTimecode(task.job.totalDurationSeconds)}`
      : "等待精确检查点";
  const selectedConfig = task.job?.parseConfig ?? task.session?.parseConfig ?? task.parseConfig;

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
        {selectedConfig ? <p className={styles.taskConfig}>
          解析配置：{selectedConfig.window.analysisMaxHeight}p · {selectedConfig.vision.fps} FPS ·
          {selectedConfig.vision.inputMode === "ordered_frames"
            ? ` 每段最多 ${selectedConfig.vision.maxFramesPerWindow} 帧`
            : "直传视频"}
        </p> : task.session ? <p className={styles.taskConfig}>历史任务未记录解析配置</p> : null}
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

function ConfigNumber({ label, value, onChange, min, max, step = 1, optional = false }: {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
  min: number;
  max: number;
  step?: number | "any";
  optional?: boolean;
}) {
  return <label className={styles.configField}>
    <span>{label}</span>
    <input type="number" className="field" value={value ?? ""} min={min} max={max} step={step}
      placeholder={optional ? "不限" : undefined}
      onChange={(event) => onChange(event.target.value === "" && optional ? null : Number(event.target.value))} />
  </label>;
}

function ConfigText({ label, value, onChange }: {
  label: string; value: string; onChange: (value: string) => void;
}) {
  return <label className={styles.configField}>
    <span>{label}</span>
    <input className="field" value={value} onChange={(event) => onChange(event.target.value)} />
  </label>;
}

function ConfigSwitch({ label, checked, onChange }: {
  label: string; checked: boolean; onChange: (value: boolean) => void;
}) {
  return <label className={styles.configSwitch}>
    <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    <span>{label}</span>
  </label>;
}

function ParseConfigFields({ value, defaults, onChange }: {
  value: ParseConfig; defaults: ParseConfig; onChange: (value: ParseConfig) => void;
}) {
  const update = <K extends Exclude<keyof ParseConfig, "version">>(section: K, patch: Partial<ParseConfig[K]>) => {
    onChange({ ...value, [section]: { ...value[section], ...patch } });
  };
  const estimatedFrames = Math.ceil(
    value.vision.fps * (value.window.chunkDurationSeconds + 2 * value.window.contextOverlapSeconds),
  );

  return <div className={styles.configSections}>
    <p className={styles.configEstimate}>
      主解析每段约 {estimatedFrames} 帧；最终 Token 和费用以模型实际返回为准。
      {value.vision.inputMode === "ordered_frames" ? `逐帧模式每段最多 ${value.vision.maxFramesPerWindow} 帧。` : "直传视频按 FPS 取帧。"}
    </p>
    <details className={styles.configGroup} open>
      <summary>切段与画面</summary>
      <div className={styles.configGrid}>
        <ConfigNumber label="每段时长（秒）" value={value.window.chunkDurationSeconds} min={configuredMin(1, defaults.window.chunkDurationSeconds)} max={configuredMax(120, defaults.window.chunkDurationSeconds)} onChange={(v) => update("window", { chunkDurationSeconds: v ?? 0 })} />
        <ConfigNumber label="上下文重叠（秒）" value={value.window.contextOverlapSeconds} min={0} max={configuredMax(30, defaults.window.contextOverlapSeconds)} onChange={(v) => update("window", { contextOverlapSeconds: v ?? 0 })} />
        <ConfigNumber label="解析输入高度（像素）" value={value.window.analysisMaxHeight} min={configuredMin(64, defaults.window.analysisMaxHeight)} max={configuredMax(2160, defaults.window.analysisMaxHeight)} onChange={(v) => update("window", { analysisMaxHeight: v ?? 0 })} />
        <ConfigNumber label="回放与入库高度（像素）" value={value.window.canonicalMaxHeight} min={configuredMin(64, defaults.window.canonicalMaxHeight)} max={configuredMax(2160, defaults.window.canonicalMaxHeight)} onChange={(v) => update("window", { canonicalMaxHeight: v ?? 0 })} />
      </div>
    </details>
    <details className={styles.configGroup} open>
      <summary>主视觉解析</summary>
      <div className={styles.configGrid}>
        <ConfigText label="Ark 主视觉模型 ID" value={value.vision.model} onChange={(v) => update("vision", { model: v })} />
        <label className={styles.configField}><span>输入方式</span><select className="field" value={value.vision.inputMode} onChange={(event) => update("vision", {
          inputMode: event.target.value as ParseConfig["vision"]["inputMode"],
          maxFramesPerWindow: event.target.value === "ordered_frames" ? 24 : null,
        })}><option value="video">直传视频</option><option value="ordered_frames">逐帧图片</option></select></label>
        <ConfigNumber label="取帧 FPS" value={value.vision.fps} min={0.2} max={5} step="any" onChange={(v) => update("vision", { fps: v ?? 0 })} />
        {value.vision.inputMode === "ordered_frames" ? <ConfigNumber label="每段最多帧数" value={value.vision.maxFramesPerWindow} min={1} max={600} onChange={(v) => update("vision", { maxFramesPerWindow: v ?? 0 })} /> : null}
        <ConfigNumber label="最大输出 Token" value={value.vision.maxOutputTokens} min={256} max={32768} optional onChange={(v) => update("vision", { maxOutputTokens: v })} />
      </div>
    </details>
    <details className={styles.configGroup}>
      <summary>ASR</summary>
      <div className={styles.configGrid}>
        <label className={styles.configField}><span>转写方式</span><select className="field" value={value.asr.mode} onChange={(event) => update("asr", { mode: event.target.value as ParseConfig["asr"]["mode"] })}><option value="lite">Lite 转写</option><option value="none">关闭 ASR</option></select></label>
        <ConfigText label="Ark ASR 模型 ID" value={value.asr.model} onChange={(v) => update("asr", { model: v })} />
        <ConfigNumber label="ASR 视频 FPS" value={value.asr.fps} min={0.2} max={5} step="any" onChange={(v) => update("asr", { fps: v ?? 0 })} />
        <ConfigNumber label="ASR 最大输出 Token" value={value.asr.maxOutputTokens} min={256} max={32768} optional onChange={(v) => update("asr", { maxOutputTokens: v })} />
        <ConfigNumber label="ASR 并发" value={value.asr.concurrency} min={1} max={configuredMax(16, defaults.asr.concurrency)} onChange={(v) => update("asr", { concurrency: v ?? 0 })} />
      </div>
    </details>
    <details className={styles.configGroup}>
      <summary>上下文与背景</summary>
      <div className={styles.configGrid}>
        <ConfigNumber label="前序事件数" value={value.context.maxEvents} min={1} max={configuredMax(100, defaults.context.maxEvents)} onChange={(v) => update("context", { maxEvents: v ?? 0 })} />
        <ConfigNumber label="前序上下文字数" value={value.context.maxChars} min={256} max={configuredMax(100000, defaults.context.maxChars)} onChange={(v) => update("context", { maxChars: v ?? 0 })} />
        <ConfigSwitch label="输入背景文字" checked={value.context.backgroundTextEnabled} onChange={(v) => update("context", { backgroundTextEnabled: v })} />
        <ConfigNumber label="背景文字最多字符" value={value.context.backgroundTextMaxChars} min={256} max={200000} optional onChange={(v) => update("context", { backgroundTextMaxChars: v })} />
        <ConfigSwitch label="输入背景参考图" checked={value.context.backgroundImagesEnabled} onChange={(v) => update("context", { backgroundImagesEnabled: v })} />
        <ConfigNumber label="最多背景参考图" value={value.context.maxBackgroundImages} min={1} max={64} optional onChange={(v) => update("context", { maxBackgroundImages: v })} />
        <ConfigNumber label="背景参考图最大边长" value={value.context.backgroundImageMaxEdge} min={configuredMin(64, defaults.context.backgroundImageMaxEdge)} max={defaults.context.backgroundImageMaxEdge} onChange={(v) => update("context", { backgroundImageMaxEdge: v ?? 0 })} />
      </div>
    </details>
    <details className={styles.configGroup}>
      <summary>内容过滤兜底</summary>
      <div className={styles.configGrid}>
        <ConfigSwitch label="启用兜底" checked={value.fallback.enabled} onChange={(v) => update("fallback", { enabled: v })} />
        <ConfigText label="DeepSeek 兜底模型 ID" value={value.fallback.model} onChange={(v) => update("fallback", { model: v })} />
        <ConfigNumber label="抽帧 FPS" value={value.fallback.frameFps} min={0.2} max={5} step="any" onChange={(v) => update("fallback", { frameFps: v ?? 0 })} />
        <ConfigNumber label="每组时长（秒）" value={value.fallback.groupDurationSeconds} min={1} max={120} onChange={(v) => update("fallback", { groupDurationSeconds: v ?? 0 })} />
        <ConfigNumber label="每组最多帧数" value={value.fallback.maxFramesPerGroup} min={1} max={128} onChange={(v) => update("fallback", { maxFramesPerGroup: v ?? 0 })} />
        <ConfigNumber label="图片最大高度" value={value.fallback.maxHeight} min={64} max={2160} onChange={(v) => update("fallback", { maxHeight: v ?? 0 })} />
        <label className={styles.configField}><span>推理强度</span><select className="field" value={value.fallback.reasoningEffort} onChange={(event) => update("fallback", { reasoningEffort: event.target.value as ParseConfig["fallback"]["reasoningEffort"] })}><option value="none">不启用</option><option value="low">低</option><option value="high">高</option><option value="max">最高</option></select></label>
        <ConfigNumber label="最大输出 Token" value={value.fallback.maxOutputTokens} min={256} max={32768} onChange={(v) => update("fallback", { maxOutputTokens: v ?? 0 })} />
        <ConfigNumber label="超时（秒）" value={value.fallback.requestTimeoutSeconds} min={configuredMin(0.01, defaults.fallback.requestTimeoutSeconds)} max={configuredMax(3600, defaults.fallback.requestTimeoutSeconds)} step="any" onChange={(v) => update("fallback", { requestTimeoutSeconds: v ?? 0 })} />
      </div>
    </details>
    <details className={styles.configGroup}>
      <summary>向量化与执行</summary>
      <div className={styles.configGrid}>
        <div className={styles.configLocked}>索引模型：{value.embedding.model} · {value.embedding.dimensions} 维</div>
        <ConfigNumber label="视频向量化 FPS" value={value.embedding.videoFps} min={0.2} max={5} step="any" onChange={(v) => update("embedding", { videoFps: v ?? 0 })} />
        <ConfigNumber label="模型请求超时（秒）" value={value.execution.requestTimeoutSeconds} min={configuredMin(0.01, defaults.execution.requestTimeoutSeconds)} max={configuredMax(3600, defaults.execution.requestTimeoutSeconds)} step="any" onChange={(v) => update("execution", { requestTimeoutSeconds: v ?? 0 })} />
        <ConfigNumber label="最多尝试次数" value={value.execution.maxAttempts} min={1} max={configuredMax(10, defaults.execution.maxAttempts)} onChange={(v) => update("execution", { maxAttempts: v ?? 0 })} />
      </div>
    </details>
  </div>;
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
  const [parseDefaults, setParseDefaults] = useState<ParseConfig | null>(null);
  const [parseConfig, setParseConfig] = useState<ParseConfig | null>(null);
  const [customConfig, setCustomConfig] = useState(false);
  const [parseConfigError, setParseConfigError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!dialogOpen) setDragging(false);
  }, [dialogOpen]);

  useEffect(() => {
    if (!dialogOpen) return;
    const controller = new AbortController();
    void getParseConfig(controller.signal).then((defaults) => {
      setParseDefaults(defaults);
      setParseConfig(defaults);
      setParseConfigError(null);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setParseConfigError(errorMessage(error));
    });
    return () => controller.abort();
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
    if (customConfig && !parseConfig) {
      setParseConfigError("解析配置尚未加载完成");
      return;
    }
    for (const item of pending) {
      const selected = item.parseConfig ?? (customConfig ? parseConfig : null);
      if (selected && parseDefaults) {
        const problem = validateParseConfig(selected, parseDefaults);
        if (problem) {
          setParseConfigError(problem);
          return;
        }
      }
    }
    setParseConfigError(null);
    enqueue(pending.map((item) => ({
      ...item,
      parseConfig: item.parseConfig ?? (customConfig ? parseConfig ?? undefined : undefined),
    })));
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

        <section className={styles.parseConfig} aria-label="解析配置">
          <div className={styles.sectionTitle}>
            <span>解析配置</span>
            <small>上传前确定；任务重试沿用同一份参数</small>
          </div>
          <div className={styles.configChoices}>
            <label><input type="radio" name="parse-config-mode" checked={!customConfig} onChange={() => setCustomConfig(false)} /> 当前默认配置</label>
            <label><input type="radio" name="parse-config-mode" checked={customConfig} disabled={!parseDefaults} onChange={() => setCustomConfig(true)} /> 自定义</label>
          </div>
          {parseDefaults ? <p className={styles.configSummary}>
            默认：{parseDefaults.window.analysisMaxHeight}p · 主解析 {parseDefaults.vision.fps} FPS · 每段 {parseDefaults.window.chunkDurationSeconds} 秒
          </p> : null}
          {parseConfigError ? <p className={styles.error} role="alert">解析配置：{parseConfigError}</p> : null}
          {customConfig && parseConfig && parseDefaults ? <>
            <ParseConfigFields value={parseConfig} defaults={parseDefaults} onChange={setParseConfig} />
            <button className={styles.resetConfig} type="button" onClick={() => setParseConfig(structuredClone(parseDefaults))}>恢复当前默认值</button>
          </> : null}
        </section>

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
                {customConfig && parseConfig ? <button className={styles.lockConfig} type="button"
                  onClick={() => setPending((current) => current.map((entry, entryIndex) =>
                    entryIndex === index ? { ...entry, parseConfig: structuredClone(parseConfig) } : entry,
                  ))}>
                  {item.parseConfig ? "已锁定本集配置" : "锁定本集配置"}
                </button> : item.parseConfig ? <span className={styles.lockedConfig}>本集独立配置</span> : null}
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
