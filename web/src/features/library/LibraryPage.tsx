import {
  ArrowRight,
  CheckCircle2,
  Clapperboard,
  Clock3,
  FolderPlus,
  RefreshCw,
  UploadCloud,
  X,
} from "lucide-react";
import { type FormEvent, useState } from "react";

import type { SeriesStatus } from "../../api/contracts";
import { mediaUrl } from "../../api/client";
import { useWorkspace } from "../../app/workspace";
import { useUploads } from "../uploads/UploadProvider";
import { episodeLabel, formatBytes, formatTimecode, stageLabel } from "../../shared/format";
import styles from "./library.module.css";

const statusLabels: Record<SeriesStatus, string> = {
  EMPTY: "等待影像",
  INDEXING: "正在整理",
  PARTIAL: "部分可检索",
  READY: "可检索",
  FAILED: "整理失败",
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
  const [failedCover, setFailedCover] = useState<string | null>(null);

  const submitSeries = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const name = String(data.get("name") ?? "").trim();
    const description = String(data.get("description") ?? "").trim();
    if (!name) return;
    setSaving(true);
    setFormError(null);
    try {
      await createSeries({ name, description: description || null });
      setCreating(false);
    } catch (creationError) {
      setFormError(creationError instanceof Error ? creationError.message : "创建系列失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <main className="page">
      <header className={styles.header}>
        <div>
          <p className="page-kicker">Local collection · 本地影像集</p>
          <h1 className="page-title">资料库</h1>
          <p className="page-description">把连续的影像整理成可追问、可回看、可验证的作品札记。</p>
        </div>
        <div className={styles.headerActions}>
          <button className="button-secondary" type="button" onClick={() => setCreating(true)}>
            <FolderPlus size={16} />新建系列
          </button>
          <button className="button-primary" type="button" onClick={openDialog} disabled={!selectedSeries}>
            <UploadCloud size={16} />上传视频
          </button>
        </div>
      </header>

      <section className={styles.shelf} aria-label="系列选择">
        <div className={styles.shelfHeading}>
          <span>系列书架 · {series.length}</span>
          <button type="button" onClick={() => void refreshSeries()} disabled={mode !== "live" || isLoading} aria-label="刷新资料库">
            <RefreshCw size={14} />
          </button>
        </div>
        {error ? <p className={styles.error}>{error.message}</p> : null}
        <div className={styles.seriesList}>
          {series.map((item, index) => (
            <button
              type="button"
              key={item.seriesId}
              className={item.seriesId === selectedSeriesId ? styles.seriesActive : styles.seriesCard}
              onClick={() => selectSeries(item.seriesId)}
            >
              <span className={styles.seriesNumber}>{String(index + 1).padStart(2, "0")}</span>
              <div>
                <strong>{item.name}</strong>
                <small>{item.videoCount} 集影像</small>
              </div>
              <span className={styles.seriesStatus} data-status={item.status}>{statusLabels[item.status]}</span>
            </button>
          ))}
          <button type="button" className={styles.addSeriesCard} onClick={() => setCreating(true)}>
            <FolderPlus size={20} strokeWidth={1.5} /><span>建立新的系列札记</span>
          </button>
        </div>
      </section>

      {selectedSeries ? (
        <>
          <section className={styles.feature}>
            <div className={styles.cover}>
              {selectedSeries.cover && failedCover !== selectedSeries.cover.imageId && mediaUrl(selectedSeries.cover.url)
                ? <img src={mediaUrl(selectedSeries.cover.url)!} alt={`${selectedSeries.name} 已登记关键帧`} onError={() => setFailedCover(selectedSeries.cover!.imageId)} />
                : <p>{failedCover === selectedSeries.cover?.imageId ? "封面图片无法读取" : "暂无已登记封面"}</p>}
              <span>{String(selectedSeries.videoCount).padStart(2, "0")}</span>
            </div>
            <div className={styles.featureCopy}>
              <span className="eyebrow">Selected series · 当前系列</span>
              <h2>{selectedSeries.name}</h2>
              <p>{selectedSeries.description || "还没有简介。上传第一集后，这里会成为整部作品的影像索引。"}</p>
              <dl>
                <div><dt>视频</dt><dd>{selectedSeries.videoCount}</dd></div>
                <div><dt>可检索 / 失败</dt><dd>{selectedSeries.readyVideoCount} / {selectedSeries.failedVideoCount}</dd></div>
                <div><dt>待上传</dt><dd>{selectedSeries.pendingUploadCount}</dd></div>
                <div><dt>人物资料</dt><dd>{selectedSeries.characterBible.length}</dd></div>
                <div><dt>状态</dt><dd>{statusLabels[selectedSeries.status]}</dd></div>
              </dl>
            </div>
            <div className={styles.featureAside}>
              <p>下一步</p>
              <strong>{selectedSeries.videoCount === 0 ? "上传第一段影像" : "继续补全剧集"}</strong>
              <span>支持一次选择多个文件，每个文件都显示独立进度。</span>
              <button type="button" onClick={openDialog}>打开传输案台 <ArrowRight size={15} /></button>
            </div>
          </section>

          <section className={styles.episodes}>
            <div className={styles.sectionHeader}>
              <div><span className="eyebrow">Indexed footage</span><h2>剧集与解析状态</h2></div>
              <span>服务端确认的资料</span>
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
                      <span>{video.parseStatus === "SUCCEEDED" ? "解析" : stageLabel(video.parseStatus)}</span><i><em style={{ width: `${video.parseProgress}%` }} /></i><b>{video.parseProgress}%</b>
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
                <Clapperboard size={24} strokeWidth={1.4} />
                <h3>这本札记还是空的</h3>
                <p>上传 2–3 个视频试试，并行进度会在同一张案台上逐条呈现。</p>
                <button className="button-primary" type="button" onClick={openDialog}><UploadCloud size={15} />开始上传</button>
              </div>
            )}
          </section>
          {selectedSeries.pendingUploads.length ? <section className={styles.episodes}><h2>尚未完成上传</h2>{selectedSeries.pendingUploads.map((upload) => <p key={upload.uploadSessionId}>{upload.originalFilename} · {upload.uploadedSizeBytes} / {upload.totalSizeBytes} 字节 · {upload.status}{upload.lastError ? ` · ${upload.lastError.message}` : ""}</p>)}</section> : null}
        </>
      ) : null}

      {creating ? (
        <div className={styles.modalBackdrop} role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setCreating(false);
        }}>
          <form className={styles.createDialog} onSubmit={submitSeries}>
            <button className={styles.modalClose} type="button" onClick={() => setCreating(false)} aria-label="关闭"><X size={18} /></button>
            <span className="page-kicker">New collection · 新札记</span>
            <h2>从一部作品开始</h2>
            <label><span>系列名称</span><input className="field" name="name" required autoFocus placeholder="例如：城市漫游第一季" /></label>
            <label><span>一句简介</span><textarea className="field" name="description" rows={4} placeholder="记录你希望从这组影像里寻找什么" /></label>
            {formError ? <p className={styles.error}>{formError}</p> : null}
            <div className={styles.modalActions}>
              <button className="button-quiet" type="button" onClick={() => setCreating(false)}>取消</button>
              <button className="button-primary" type="submit" disabled={saving}>{saving ? "正在建立…" : "建立系列"}</button>
            </div>
          </form>
        </div>
      ) : null}
    </main>
  );
}
