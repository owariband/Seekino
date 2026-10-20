import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Clapperboard, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { errorMessage, getSeries, mediaUrl, shouldRetryApiRequest } from "../../api/client";
import type { SeriesSummary } from "../../api/contracts";
import { useWorkspace } from "../../app/workspace";
import { useAuth } from "../auth/AuthProvider";
import { SeriesActionsMenu } from "../library/SeriesActionsMenu";
import { seriesPath, watchPath } from "../library/seriesLinks";
import { episodeTitle, formatTimecode, previewFramePosition, seriesTitle } from "../../shared/format";
import { Mascot } from "../../shared/Mascot";
import { readWatchHistory } from "./playbackProgress";
import { PlaybackFrame } from "./PlaybackFrame";
import { useI18n } from "../../app/i18n";
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
    queryKey: ["series-detail", series.seriesId, series.tenantId], queryFn: () => getSeries(series.seriesId, series.tenantId),
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
  const { t, locale } = useI18n();
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
    queryKey: ["series-detail", activeSeriesId, selectedSeries?.tenantId],
    queryFn: () => getSeries(activeSeriesId!, selectedSeries!.tenantId),
    enabled: Boolean(selectedSeries),
    retry: shouldRetryApiRequest,
  });
  const episodes = [...(detailQuery.data?.videos ?? [])].sort((left, right) =>
    left.episodeOrder - right.episodeOrder || left.uploadSequence - right.uploadSequence,
  );
  const firstPlayable = episodes.find((item) => item.playbackStatus === "READY" && item.playbackUrl);
  const recentSeries = recent ? series.find((item) => item.seriesId === recent.seriesId) : null;
  const recentDetail = useQuery({
    queryKey: ["series-detail", recent?.seriesId, recentSeries?.tenantId], queryFn: () => getSeries(recent!.seriesId, recentSeries!.tenantId),
    enabled: Boolean(recentSeries), retry: shouldRetryApiRequest,
  });
  const recentVideo = recentDetail.data?.videos.find((item) => item.videoId === recent?.videoId && item.playbackStatus === "READY" && item.playbackUrl);
  const heroSeries = recentVideo ? recentSeries : selectedSeries;
  const heroVideo = recentVideo ?? firstPlayable;
  const heroPosition = recentVideo && recent ? recent.positionSeconds : Math.min(30_000, (heroVideo?.durationSeconds ?? 0) * 0.1);

  return (
    <main className={`page ${styles.home}`} aria-label={t("视频观看")}>
      <h1 className="sr-only">{t("视频观看")}</h1>

      {heroSeries ? (
        <section className={styles.hero} aria-label={t("继续观看推荐")}>
          <PlaybackFrame className={styles.heroFrame} eager src={mediaUrl(heroVideo?.playbackUrl ?? null)} positionSeconds={heroPosition}
            poster={mediaUrl(heroVideo?.thumbnail?.url ?? heroSeries.cover?.url ?? null)} />
          <div className={styles.heroContent}>
            <span className={styles.heroKicker}>{recentVideo ? t("继续上次的故事") : t("从这里开始")}</span>
            <h2>{seriesTitle(heroSeries)}</h2>
            <div className={styles.heroMeta}>
              {heroVideo ? <span>{episodeTitle(heroVideo.episodeOrder, locale)}</span> : null}
              <span>{t("{count} 集", { count: heroSeries.videoCount })}</span>
              {recentVideo && recent ? <span>{t("上次看到 {time}", { time: formatTimecode(recent.positionSeconds) })}</span> : null}
            </div>
            <p>{heroSeries.description || (recentVideo ? t("故事还在继续，回到上次停下的那一刻。") : t("选一集开始观看，也可以一起寻找故事里的名场面。"))}</p>
            <div className={styles.heroActions}>
              {heroVideo ? <Link className={styles.heroPlay} to={watchPath(heroSeries, heroVideo.videoId)}><Play size={20} fill="currentColor" aria-hidden="true" />{recentVideo ? t("继续观看") : t("播放本集")}</Link> : null}
              <Link className={styles.heroBrowse} to={seriesPath(heroSeries)}>{t("查看系列")}<ChevronRight size={16} aria-hidden="true" /></Link>
            </div>
            {recentVideo && recent && heroVideo && heroVideo.durationSeconds > 0 ? <div className={styles.heroProgress}>
              <progress aria-label={t("上次观看进度")} max={heroVideo.durationSeconds} value={Math.min(recent.positionSeconds, heroVideo.durationSeconds)} />
              <span>{formatTimecode(recent.positionSeconds)} / {formatTimecode(heroVideo.durationSeconds)}</span>
            </div> : null}
          </div>
        </section>
      ) : null}

      {error ? <p className={styles.homeError} role="alert"><Mascot mood="oops" size={56} /><span>{t("读取视频系列失败：{message}", { message: errorMessage(error, locale) })}</span></p> : null}
      {isLoading ? <p className={styles.homeEmpty} role="status"><Mascot mood="seek" size={56} delayed /><span>{t("正在读取视频系列…")}</span></p> : null}
      {!isLoading && !error && series.length === 0 ? (
        <div className={styles.homeEmpty}>
          <Mascot mood="puzzled" size={72} />
          <span>{activeSource.kind === "public" ? t("公开资料里还没有视频。") : t("资料库里还没有视频。")}</span>
          {canUpload ? <Link to="/library">{t("前往资料库上传视频")}</Link> : null}
        </div>
      ) : null}

      {series.length > 0 ? (
        <div className={styles.homeGrid}>
          <section className={styles.seriesShelf} aria-label={t("选择视频系列")}>
            <div className={styles.shelfHeading}>
              <h2>{t("视频系列")} <small>{t("{count} 部作品", { count: series.length })}</small></h2>
              {shelfEdges.previous || shelfEdges.next ? <div className={styles.shelfControls}>
                <button type="button" aria-label={t("上一组系列")} aria-controls="watch-series-shelf" disabled={!shelfEdges.previous} onClick={() => scrollShelf(-1)}><ChevronLeft size={18} aria-hidden="true" /></button>
                <button type="button" aria-label={t("下一组系列")} aria-controls="watch-series-shelf" disabled={!shelfEdges.next} onClick={() => scrollShelf(1)}><ChevronRight size={18} aria-hidden="true" /></button>
              </div> : null}
            </div>
            <div id="watch-series-shelf" className={styles.seriesCards} ref={shelfRef}>
              {series.map((item) => (
                <article key={item.seriesId} className={styles.seriesCard}>
                  <Link className={styles.seriesCardLink} to={seriesPath(item)}>
                    <SeriesArtwork series={item} />
                    <span><strong>{seriesTitle(item)}</strong><small>{t("{count} 集 · 查看系列", { count: item.videoCount })}</small></span>
                  </Link>
                  <div className={styles.seriesCardFooter}>
                    {item.visibility === "public" && item.publisher ? <span className={styles.publisher}>
                      <span className={styles.publisherAvatar} aria-hidden="true">{item.publisher.displayName.slice(0, 1).toUpperCase()}
                        {item.publisher.avatar ? <img src={mediaUrl(item.publisher.avatar.url) ?? undefined} alt="" onError={(event) => { event.currentTarget.hidden = true; }} /> : null}
                      </span>
                      <small>{t("{name} 分享", { name: item.publisher.displayName })}</small>
                    </span> : <span />}
                    <SeriesActionsMenu series={item} />
                  </div>
                </article>
              ))}
            </div>
          </section>

        </div>
      ) : null}
    </main>
  );
}
