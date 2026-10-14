import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Clapperboard, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { getSeries, mediaUrl, shouldRetryApiRequest } from "../../api/client";
import type { SeriesSummary } from "../../api/contracts";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
import { episodeTitle, formatTimecode, previewFramePosition, seriesTitle } from "../../shared/format";
import { Mascot } from "../../shared/Mascot";
import { readWatchHistory } from "./playbackProgress";
import { PlaybackFrame } from "./PlaybackFrame";
import styles from "./watch.module.css";

function SeriesArtwork({ series }: { series: SeriesSummary }) {
  const containerRef = useRef<HTMLSpanElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!containerRef.current) return;
    if (typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    const observer = new IntersectionObserver(([entry]) => {
      if (entry?.isIntersecting) { setVisible(true); observer.disconnect(); }
    }, { rootMargin: "160px" });
    observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, []);
  const detail = useQuery({
    queryKey: ["series-detail", series.seriesId], queryFn: () => getSeries(series.seriesId),
    enabled: visible && !series.cover, retry: shouldRetryApiRequest,
  });
  const playable = [...(detail.data?.videos ?? [])].filter((video) => video.playbackStatus === "READY" && video.playbackUrl)
    .sort((a, b) => a.episodeOrder - b.episodeOrder || a.uploadSequence - b.uploadSequence);
  const video = playable[0];
  return <span className={styles.seriesCover} ref={containerRef}>
    <span className={styles.coverFallback} aria-hidden="true"><Clapperboard size={30} strokeWidth={1} /><b>{seriesTitle(series)}</b></span>
    {video && !series.cover ? <PlaybackFrame className={styles.seriesFrame}
      src={mediaUrl(video.playbackUrl)} positionSeconds={previewFramePosition(video.durationSeconds)}
      poster={mediaUrl(video.thumbnail?.url ?? null)} />
      : series.cover ? <img key={series.cover.url} src={mediaUrl(series.cover.url) ?? undefined} alt="" loading="lazy" onError={(event) => { event.currentTarget.hidden = true; }} /> : null}
  </span>;
}

export function WatchHomePage() {
  const { series, isLoading, error } = useWorkspace();
  const { activeSource, canUpload } = useAuth();
  const shelfRef = useRef<HTMLDivElement>(null);
  const [shelfEdges, setShelfEdges] = useState({ previous: false, next: false });
  useEffect(() => {
    const shelf = shelfRef.current;
    if (!shelf) return;
    const updateEdges = () => setShelfEdges({
      previous: shelf.scrollLeft > 1,
      next: shelf.scrollLeft + shelf.clientWidth < shelf.scrollWidth - 1,
    });
    updateEdges();
    shelf.addEventListener("scroll", updateEdges, { passive: true });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(updateEdges);
    observer?.observe(shelf);
    return () => { shelf.removeEventListener("scroll", updateEdges); observer?.disconnect(); };
  }, [series.length]);
  const scrollShelf = (direction: number) => {
    const shelf = shelfRef.current;
    shelf?.scrollBy({ left: direction * shelf.clientWidth * 0.85, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
  };
  const history = readWatchHistory();
  const recent = history.find((item) => series.some((entry) => entry.seriesId === item.seriesId));
  const activeSeriesId = recent?.seriesId ?? series[0]?.seriesId ?? null;
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
  const recentDetail = useQuery({
    queryKey: ["series-detail", recent?.seriesId], queryFn: () => getSeries(recent!.seriesId),
    enabled: Boolean(recent), retry: shouldRetryApiRequest,
  });
  const recentVideo = recentDetail.data?.videos.find((item) => item.videoId === recent?.videoId && item.playbackStatus === "READY" && item.playbackUrl);
  const heroSeries = recentVideo ? recentSeries : selectedSeries;
  const heroVideo = recentVideo ?? firstPlayable;
  const heroPosition = recentVideo && recent ? recent.positionSeconds : Math.min(30_000, (heroVideo?.durationSeconds ?? 0) * 0.1);

  return (
    <main className={`page ${styles.home}`} aria-label="视频观看">
      <h1 className="sr-only">视频观看</h1>

      {heroSeries ? (
        <section className={styles.hero} aria-label="继续观看推荐">
          <PlaybackFrame className={styles.heroFrame} eager src={mediaUrl(heroVideo?.playbackUrl ?? null)} positionSeconds={heroPosition}
            poster={mediaUrl(heroVideo?.thumbnail?.url ?? heroSeries.cover?.url ?? null)} />
          <div className={styles.heroContent}>
            <span className={styles.heroKicker}>{recentVideo ? "继续上次的故事" : "从这里开始"}</span>
            <h2>{seriesTitle(heroSeries)}</h2>
            <div className={styles.heroMeta}>
              {heroVideo ? <span>{episodeTitle(heroVideo.episodeOrder)}</span> : null}
              <span>{heroSeries.videoCount} 集</span>
              {recentVideo && recent ? <span>上次看到 {formatTimecode(recent.positionSeconds)}</span> : null}
            </div>
            <p>{heroSeries.description || (recentVideo ? "故事还在继续，回到上次停下的那一刻。" : "选一集开始观看，也可以一起寻找故事里的名场面。")}</p>
            <div className={styles.heroActions}>
              {heroVideo ? <Link className={styles.heroPlay} to={`/watch/${encodeURIComponent(heroVideo.seriesId)}/${encodeURIComponent(heroVideo.videoId)}`}><Play size={20} fill="currentColor" aria-hidden="true" />{recentVideo ? "继续观看" : "播放本集"}</Link> : null}
              <Link className={styles.heroBrowse} to={`/series/${encodeURIComponent(heroSeries.seriesId)}`}>查看系列<ChevronRight size={16} aria-hidden="true" /></Link>
            </div>
            {recentVideo && recent && heroVideo && heroVideo.durationSeconds > 0 ? <div className={styles.heroProgress}>
              <progress aria-label="上次观看进度" max={heroVideo.durationSeconds} value={Math.min(recent.positionSeconds, heroVideo.durationSeconds)} />
              <span>{formatTimecode(recent.positionSeconds)} / {formatTimecode(heroVideo.durationSeconds)}</span>
            </div> : null}
          </div>
        </section>
      ) : null}

      {error ? <p className={styles.homeError} role="alert"><Mascot mood="oops" size={56} /><span>读取视频系列失败：{error.message}</span></p> : null}
      {isLoading ? <p className={styles.homeEmpty} role="status"><Mascot mood="seek" size={56} delayed /><span>正在读取视频系列…</span></p> : null}
      {!isLoading && !error && series.length === 0 ? (
        <div className={styles.homeEmpty}>
          <Mascot mood="puzzled" size={72} />
          <span>{activeSource.kind === "public" ? "公开资料里还没有视频。" : "资料库里还没有视频。"}</span>
          {canUpload ? <Link to="/library">前往资料库上传视频</Link> : null}
        </div>
      ) : null}

      {series.length > 0 ? (
        <div className={styles.homeGrid}>
          <section className={styles.seriesShelf} aria-label="选择视频系列">
            <div className={styles.shelfHeading}>
              <h2>视频系列 <small>{series.length} 部作品</small></h2>
              {shelfEdges.previous || shelfEdges.next ? <div className={styles.shelfControls}>
                <button type="button" aria-label="上一组系列" aria-controls="watch-series-shelf" disabled={!shelfEdges.previous} onClick={() => scrollShelf(-1)}><ChevronLeft size={18} aria-hidden="true" /></button>
                <button type="button" aria-label="下一组系列" aria-controls="watch-series-shelf" disabled={!shelfEdges.next} onClick={() => scrollShelf(1)}><ChevronRight size={18} aria-hidden="true" /></button>
              </div> : null}
            </div>
            <div id="watch-series-shelf" className={styles.seriesCards} ref={shelfRef}>
              {series.map((item) => (
                <Link key={item.seriesId} className={styles.seriesCard} style={{ textDecoration: "none" }} to={`/series/${encodeURIComponent(item.seriesId)}`}>
                  <SeriesArtwork series={item} />
                  <span><strong>{seriesTitle(item)}</strong><small>{item.videoCount} 集 · 查看系列</small></span>
                </Link>
              ))}
            </div>
          </section>

        </div>
      ) : null}
    </main>
  );
}
