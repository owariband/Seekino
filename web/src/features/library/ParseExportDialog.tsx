import { useQuery } from "@tanstack/react-query";
import { Download, X } from "lucide-react";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { errorMessage, exportParsedVideos, getParseExportOptions, getSeries, shouldRetryApiRequest } from "../../api/client";
import type { ParseExportSummary, SeriesSummary } from "../../api/contracts";
import { useI18n } from "../../app/i18n";
import { episodeTitle, seriesTitle } from "../../shared/format";
import styles from "./ParseExportDialog.module.css";

// These are display labels only; selectable types come from the formal server contract.
const eventLabels: Record<string, string> = {
  SCENE: "场景", ACTION: "动作", DIALOGUE: "对白", EMOTION: "情绪",
  RELATION_CHANGE: "关系变化", ON_SCREEN_TEXT: "画面文字", OTHER: "其他",
};

export function ParseExportDialog({ series, onClose }: { series: SeriesSummary; onClose: () => void }) {
  const { t, locale } = useI18n();
  const titleId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const controller = useRef<AbortController | null>(null);
  const [videoIds, setVideoIds] = useState<string[] | null>(null);
  const [eventTypes, setEventTypes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<ParseExportSummary | null>(null);
  const detail = useQuery({
    queryKey: ["series-detail", series.seriesId, series.tenantId],
    queryFn: () => getSeries(series.seriesId, series.tenantId), retry: shouldRetryApiRequest,
  });
  const options = useQuery({
    queryKey: ["parse-export-options", series.tenantId],
    queryFn: ({ signal }) => getParseExportOptions(series.tenantId, signal), retry: shouldRetryApiRequest,
  });
  const videos = [...(detail.data?.videos ?? [])].sort((a, b) => a.episodeOrder - b.episodeOrder || a.uploadSequence - b.uploadSequence);
  const availableTypes = options.data?.eventTypes ?? [];
  const selectedVideos = videos.map((video) => video.videoId).filter((id) => videoIds === null || videoIds.includes(id));
  const selectedTypes = availableTypes.filter((type) => eventTypes === null || eventTypes.includes(type));
  const loading = detail.isPending || options.isPending;
  const loadError = detail.error ?? options.error;

  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
    return () => { controller.current?.abort(); controller.current = null; };
  }, []);

  const close = () => { dialog.current?.close(); onClose(); };

  const download = async (event: FormEvent) => {
    event.preventDefault();
    if (controller.current || loading || loadError || !selectedVideos.length || !selectedTypes.length) return;
    const request = new AbortController();
    controller.current = request;
    setBusy(true);
    setError(null);
    setSummary(null);
    try {
      const result = await exportParsedVideos({ tenantId: series.tenantId, videoIds: selectedVideos, eventTypes: selectedTypes }, request.signal);
      request.signal.throwIfAborted();
      if (result.summary.record_count > 0) {
        const url = URL.createObjectURL(result.blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = result.filename;
        document.body.append(link);
        try { link.click(); }
        finally { link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 0); }
      }
      setSummary(result.summary);
    } catch (failure) {
      if (!request.signal.aborted && !(failure instanceof DOMException && failure.name === "AbortError")) setError(errorMessage(failure, locale));
    } finally {
      if (controller.current === request) { controller.current = null; setBusy(false); }
    }
  };

  return <dialog ref={dialog} className={styles.dialog} aria-labelledby={titleId}
    onCancel={(event) => { event.preventDefault(); close(); }}>
    <header className={styles.header}>
      <div><h2 id={titleId}>{t("导出解析数据")}</h2><p>{seriesTitle(series)}</p></div>
      <button className="button-quiet" type="button" aria-label={t("关闭导出")} onClick={close}><X size={20} aria-hidden="true" /></button>
    </header>
    <p className={styles.hint}>{t("导出已有的角色对白与剧情描述，保存为 JSONL。")}</p>
    {loading ? <p role="status">{t("正在读取导出选项…")}</p> : null}
    {loadError ? <div role="alert" className={styles.error}><p>{errorMessage(loadError, locale)}</p>
      <button className="button-secondary" type="button" onClick={() => { void detail.refetch(); void options.refetch(); }}>{t("重试")}</button>
    </div> : null}
    <form onSubmit={(event) => void download(event)}>
      <fieldset disabled={busy || loading || Boolean(loadError)}>
        <legend>{t("选择视频")}</legend>
        <div className={styles.controls}>
          <button type="button" className="button-quiet" onClick={() => setVideoIds(null)}>{t("全选视频")}</button>
          <button type="button" className="button-quiet" onClick={() => setVideoIds([])}>{t("清空视频")}</button>
          <span>{t("已选 {count} 个视频", { count: selectedVideos.length })}</span>
        </div>
        <div className={styles.videos}>
          {videos.map((video) => <label key={video.videoId} className={styles.choice}>
            <input type="checkbox" checked={selectedVideos.includes(video.videoId)} onChange={(event) => {
              setVideoIds(event.target.checked ? [...selectedVideos, video.videoId] : selectedVideos.filter((id) => id !== video.videoId));
            }} />
            <span>{episodeTitle(video.episodeOrder, locale)}<small>{video.originalFilename ?? video.videoId}</small></span>
          </label>)}
        </div>
        {!loading && !loadError && !videos.length ? <p>{t("这个系列还没有视频。")}</p> : null}
        <p className={styles.hint}>{t("下载时检查正式解析结果；尚未完成的内容会说明原因。")}</p>
      </fieldset>
      <fieldset disabled={busy || loading || Boolean(loadError)}>
        <legend>{t("选择内容类型")}</legend>
        <div className={styles.controls}>
          <button type="button" className="button-quiet" onClick={() => setEventTypes(null)}>{t("全选类型")}</button>
          <button type="button" className="button-quiet" disabled={!availableTypes.includes("DIALOGUE")} onClick={() => setEventTypes(["DIALOGUE"])}>{t("仅对白")}</button>
        </div>
        <div className={styles.types}>
          {availableTypes.map((type) => <label key={type} className={styles.choice}>
            <input type="checkbox" checked={selectedTypes.includes(type)} onChange={(event) => {
              setEventTypes(event.target.checked ? [...selectedTypes, type] : selectedTypes.filter((value) => value !== type));
            }} />{t(eventLabels[type] ?? type)}
          </label>)}
        </div>
      </fieldset>
      {error ? <p role="alert" className={styles.error}>{error}</p> : null}
      {summary ? <div role="status" className={styles.result}>
        <p>{summary.record_count ? t("已开始下载：{records} 条记录，{events} 个事件。", { records: summary.record_count, events: summary.event_count })
          : t("没有符合所选类型的事件，未生成下载文件。")}</p>
        <p>{t("跳过 {count} 个无匹配事件的片段。", { count: summary.skipped.no_matching_events })}</p>
      </div> : null}
      <footer className={styles.footer}>
        <button className="button-secondary" type="button" onClick={close}>{t("关闭")}</button>
        <button className="button-primary" type="submit" disabled={busy || loading || Boolean(loadError) || !selectedVideos.length || !selectedTypes.length}>
          <Download size={16} aria-hidden="true" />{busy ? t("正在导出…") : t("下载 JSONL")}
        </button>
      </footer>
    </form>
  </dialog>;
}
