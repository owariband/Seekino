import {
  CheckCircle2,
  Clapperboard,
  Clock3,
  FolderPlus,
  RefreshCw,
  UploadCloud,
  X,
} from "lucide-react";
import { type FormEvent, useState } from "react";

import { useWorkspace } from "../../app/workspace";
import { useUploads } from "../uploads/UploadProvider";
import { episodeLabel, formatBytes, formatTimecode } from "../../shared/format";
import { SeriesBackgroundEditor } from "./SeriesBackgroundEditor";
import styles from "./library.module.css";

const parseStatusLabels: Record<string, string> = {
  QUEUED: "等待解析",
  RUNNING: "解析中",
  RETRY_WAIT: "等待重试",
  SUCCEEDED: "解析",
  FAILED: "解析失败",
};

export function LibraryPage() {
  const {
    mode,
    series,
    selectedSeries,
    selectedSeriesId,
    selectSeries,
    createSeries,
    refreshSeries,
    isLoading,
    error,
  } = useWorkspace();
  const { openDialog } = useUploads();
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const submitSeries = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const name = String(data.get("name") ?? "").trim();
    const description = String(data.get("description") ?? "").trim();
    const seriesBible = String(data.get("seriesBible") ?? "");
    if (!name) return;
    setSaving(true);
    setFormError(null);
    try {
      await createSeries({ name, description: description || null, importantExtraData: seriesBible.trim() ? { format: "markdown", content: seriesBible } : null });
      setCreating(false);
    } catch (creationError) {
      setFormError(creationError instanceof Error ? creationError.message : "创建视频系列失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="page">
      <header className={styles.header}>
        <div>
          <h1 className="page-title">资料库</h1>
        </div>
        <div className={styles.headerActions}>
          <button className="button-secondary" type="button" onClick={() => setCreating(true)}>
            <FolderPlus size={16} />新建视频系列
          </button>
          <button className="button-primary" type="button" onClick={openDialog} disabled={!selectedSeries}>
            <UploadCloud size={16} />上传视频
          </button>
        </div>
      </header>

      <section className={styles.shelf} aria-label="视频系列选择">
        <div className={styles.shelfHeading}>
          <span>视频系列</span>
          <button type="button" onClick={() => void refreshSeries()} disabled={mode !== "live" || isLoading} aria-label="刷新资料库">
            <RefreshCw size={14} />
          </button>
        </div>
        {error ? <p className={styles.error} role="alert">{error.message}</p> : null}
        <div className={styles.seriesList}>
          {series.map((item) => (
            <button
              type="button"
              key={item.seriesId}
              className={item.seriesId === selectedSeriesId ? styles.seriesActive : styles.seriesCard}
              onClick={() => selectSeries(item.seriesId)}
              aria-pressed={item.seriesId === selectedSeriesId}
            >
              {item.name}
            </button>
          ))}
        </div>
        {!isLoading && !error && series.length === 0 ? <p className={styles.emptySeries}>暂无视频系列，点击“新建视频系列”开始。</p> : null}
        {isLoading ? <p className={styles.emptySeries} role="status">正在读取视频系列…</p> : null}
      </section>

      {selectedSeries ? (
        <>
          <SeriesBackgroundEditor key={`${selectedSeries.workspaceId}:${selectedSeries.seriesId}`} series={selectedSeries} />

          <section className={styles.episodes}>
            <div className={styles.sectionHeader}>
              <h2>视频</h2>
              <span>{selectedSeries.videos.length} 个视频</span>
            </div>
            {selectedSeries.videos.length > 0 ? (
              <div className={styles.episodeList}>
                {selectedSeries.videos.map((video) => (
                  <article className={styles.episode} key={video.videoId}>
                    <div className={styles.episodeOrder}>{episodeLabel(video.episodeOrder)}</div>
                    <div className={styles.episodeName}>
                      <Clapperboard size={16} />
                      <div><strong>{video.originalFilename}</strong><span>{formatBytes(video.totalBytes)} · <Clock3 size={11} /> {formatTimecode(video.durationMs)}</span></div>
                    </div>
                    <div className={styles.progressCell}>
                      <span>上传</span><i><em style={{ width: `${video.uploadProgress}%` }} /></i><b>{video.uploadProgress}%</b>
                    </div>
                    <div className={styles.progressCell}>
                      <span>{parseStatusLabels[video.parseStatus] ?? "解析"}</span><i><em style={{ width: `${video.parseProgress}%` }} /></i><b>{video.parseProgress}%</b>
                    </div>
                    <div className={styles.episodeReady} data-ready={video.indexStatus === "READY"}>
                      {video.indexStatus === "READY" ? <CheckCircle2 size={15} /> : <Clock3 size={15} />}
                      {video.indexStatus === "READY" ? "可检索" : video.indexStatus === "FAILED" ? "索引失败" : "索引未就绪"}
                      <small>{video.playbackStatus === "READY" ? "可播放" : video.playbackStatus === "MISSING" ? "原视频已丢失" : "播放未就绪"}</small>
                      {video.lastError ? <small>{video.lastError.message}</small> : null}
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <div className={styles.emptyEpisodes}>
                <p>暂无视频，点击“上传视频”添加。</p>
              </div>
            )}
          </section>
          {selectedSeries.pendingUploads.length ? <section className={styles.episodes} aria-label="上传中的视频"><div className={styles.sectionHeader}><h2>上传中</h2></div><div className={styles.pendingUploads}>{selectedSeries.pendingUploads.map((upload) => <article key={upload.uploadSessionId}><strong>{upload.originalFilename}</strong><span>{upload.status === "COMPLETING" ? "正在确认" : upload.status === "EXPIRED" ? "已过期" : upload.status === "COMPLETED" ? "已上传" : "上传中"}</span><progress aria-label={`${upload.originalFilename} 上传进度`} value={upload.uploadedSizeBytes} max={upload.totalSizeBytes} /><small>{formatBytes(upload.uploadedSizeBytes)} / {formatBytes(upload.totalSizeBytes)}</small>{upload.lastError ? <p className={styles.error}>{upload.lastError.message}</p> : null}</article>)}</div></section> : null}
        </>
      ) : null}

      {creating ? (
        <div className={styles.modalBackdrop} role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setCreating(false);
        }}>
          <form className={styles.createDialog} onSubmit={submitSeries} role="dialog" aria-modal="true" aria-label="新建视频系列">
            <button className={styles.modalClose} type="button" onClick={() => setCreating(false)} aria-label="关闭"><X size={18} /></button>
            <h2>新建视频系列</h2>
            <label><span>视频系列名称</span><input className="field" name="name" required autoFocus placeholder="例如：城市漫游第一季" /></label>
            <label><span>简介（选填）</span><input className="field" name="description" placeholder="简单介绍这个视频系列" /></label>
            <label><span>背景资料（选填）</span><textarea className="field" name="seriesBible" rows={5} placeholder="人物关系、别名、剧情背景等，帮助解析与检索。" /></label>
            {formError ? <p className={styles.error}>{formError}</p> : null}
            <div className={styles.modalActions}>
              <button className="button-quiet" type="button" onClick={() => setCreating(false)}>取消</button>
              <button className="button-primary" type="submit" disabled={saving}>{saving ? "正在创建…" : "创建视频系列"}</button>
            </div>
          </form>
        </div>
      ) : null}
    </main>
  );
}
