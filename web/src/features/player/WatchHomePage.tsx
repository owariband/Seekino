import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Clapperboard, Play } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { errorMessage, getSeries, mediaUrl, shouldRetryApiRequest } from "../../api/client";
import { useWorkspace } from "../../app/workspace";
import { episodeLabel, formatTimecode } from "../../shared/format";
import { readVideoProgress, readWatchHistory } from "./playbackProgress";
import styles from "./watch.module.css";

export function WatchHomePage() {
  const { series, isLoading, error } = useWorkspace();
  const [requestedSeriesId, setRequestedSeriesId] = useState<string | null>(null);
  const history = readWatchHistory();
  const recent = history.find((item) => series.some((entry) => entry.seriesId === item.seriesId));
  const activeSeriesId = requestedSeriesId && series.some((item) => item.seriesId === requestedSeriesId)
    ? requestedSeriesId : recent?.seriesId ?? series[0]?.seriesId ?? null;
  const selectedSeries = series.find((item) => item.seriesId === activeSeriesId);
  const detailQuery = useQuery({
    queryKey: ["series-detail", activeSeriesId],
    queryFn: () => getSeries(activeSeriesId!),
    enabled: Boolean(activeSeriesId),
    retry: shouldRetryApiRequest,
  });
  const episodes = [...(detailQuery.data?.videos ?? [])].sort((left, right) =>
    left.episodeOrder - right.episodeOrder || left.uploadSequence - right.uploadSequence,
  );
  const firstPlayable = episodes.find((item) => item.playbackStatus === "READY" && item.playbackUrl);
  const recentSeries = recent ? series.find((item) => item.seriesId === recent.seriesId) : null;
  const recentVideo = recent?.seriesId === activeSeriesId
    ? episodes.find((item) => item.videoId === recent.videoId) : null;

  return (
    <main className={`page ${styles.home}`}>
      <header className={styles.homeHeader}>
        <span>ANI VAG / WATCH</span>
        <h1>视频观看</h1>
        <p>选择一部视频，从上次离开的地方继续。</p>
      </header>

      {recent && recentSeries ? (
        <Link className={styles.continueCard} to={`/watch/${encodeURIComponent(recent.seriesId)}/${encodeURIComponent(recent.videoId)}`}>
          <span className={styles.continueIcon}><Play size={19} fill="currentColor" aria-hidden="true" /></span>
          <span><small>继续观看</small><strong>{recentSeries.name}{recentVideo ? ` · ${episodeLabel(recentVideo.episodeOrder)}` : ""}</strong><em>上次看到 {formatTimecode(recent.positionMs)}</em></span>
          <ChevronRight size={20} aria-hidden="true" />
        </Link>
      ) : null}

      {error ? <p className={styles.homeError} role="alert">读取视频系列失败：{error.message}</p> : null}
      {isLoading ? <p className={styles.homeEmpty} role="status">正在读取视频系列…</p> : null}
      {!isLoading && !error && series.length === 0 ? (
        <div className={styles.homeEmpty}>资料库里还没有视频。<Link to="/library">前往资料库上传视频</Link></div>
      ) : null}

      {series.length > 0 ? (
        <div className={styles.homeGrid}>
          <section className={styles.seriesShelf} aria-label="选择视频系列">
            <h2>视频系列 <small>{series.length}</small></h2>
            <div className={styles.seriesCards}>
              {series.map((item) => (
                <button key={item.seriesId} type="button" className={item.seriesId === activeSeriesId ? styles.seriesCardActive : styles.seriesCard} onClick={() => setRequestedSeriesId(item.seriesId)} aria-pressed={item.seriesId === activeSeriesId}>
                  <span className={styles.seriesCover}>
                    {item.cover ? <img src={mediaUrl(item.cover.url) ?? undefined} alt="" /> : <Clapperboard size={22} strokeWidth={1.4} aria-hidden="true" />}
                  </span>
                  <span><strong>{item.name}</strong><small>{item.videoCount} 集</small></span>
                </button>
              ))}
            </div>
          </section>

          <section className={styles.homeEpisodes} aria-label="系列选集">
            <div className={styles.homeEpisodesHeader}>
              <div><small>正在浏览</small><h2>{selectedSeries?.name ?? "视频系列"}</h2><p>{selectedSeries?.description || "选择一集开始观看。"}</p></div>
              {firstPlayable && activeSeriesId ? <Link className="button-primary" to={`/watch/${encodeURIComponent(activeSeriesId)}/${encodeURIComponent(firstPlayable.videoId)}`}><Play size={15} fill="currentColor" aria-hidden="true" />开始观看</Link> : null}
            </div>
            {detailQuery.isPending ? <p className={styles.homeEmpty} role="status">正在读取选集…</p> : null}
            {detailQuery.isError ? <p className={styles.homeError} role="alert">读取选集失败：{errorMessage(detailQuery.error)}</p> : null}
            {detailQuery.isSuccess && episodes.length === 0 ? <p className={styles.homeEmpty}>这个系列还没有视频。</p> : null}
            <ol className={styles.homeEpisodeList}>
              {episodes.map((episode) => {
                const progress = readVideoProgress(episode.videoId);
                return (
                  <li key={episode.videoId}>
                    <Link className={styles.homeEpisode} to={`/watch/${encodeURIComponent(episode.seriesId)}/${encodeURIComponent(episode.videoId)}`}>
                      <span className={styles.homeEpisodeNumber}>{episodeLabel(episode.episodeOrder)}</span>
                      <span className={styles.homeEpisodeTitle}><strong>{episode.originalFilename}</strong><small>{formatTimecode(episode.durationMs)}{progress && progress.positionMs > 0 ? ` · 已看至 ${formatTimecode(progress.positionMs)}` : ""}</small></span>
                      <span className={styles.homeEpisodeAction}>{episode.playbackStatus === "READY" ? "观看" : "查看状态"}<ChevronRight size={16} aria-hidden="true" /></span>
                      {progress && episode.durationMs > 0 ? <span className={styles.homeEpisodeProgress} style={{ width: `${Math.min(100, progress.positionMs / episode.durationMs * 100)}%` }} /> : null}
                    </Link>
                  </li>
                );
              })}
            </ol>
          </section>
        </div>
      ) : null}
    </main>
  );
}
