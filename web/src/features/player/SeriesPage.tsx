import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ChevronDown, Clapperboard, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router";

import { errorMessage, getSeries, mediaUrl, shouldRetryApiRequest } from "../../api/client";
import { episodeLabel, episodeTitle, formatTimecode, seriesTitle } from "../../shared/format";
import { Mascot } from "../../shared/Mascot";
import { useAuth } from "../auth/AuthProvider";
import { readBackground, SeriesBackgroundEditor } from "../library/SeriesBackgroundEditor";
import { PlaybackFrame } from "./PlaybackFrame";
import { readWatchHistory } from "./playbackProgress";
import styles from "./series.module.css";

export function SeriesPage() {
  const { seriesId } = useParams();
  const pageRef = useRef<HTMLElement>(null);
  const { canManage } = useAuth();
  const [watchHistory, setWatchHistory] = useState(() => readWatchHistory());
  const seriesQuery = useQuery({
    queryKey: ["series-detail", seriesId], queryFn: () => getSeries(seriesId!),
    enabled: Boolean(seriesId), retry: shouldRetryApiRequest,
  });
  useEffect(() => {
    // The departing player saves its final position during effect cleanup.
    setWatchHistory(readWatchHistory());
    pageRef.current?.scrollIntoView?.({ block: "start", behavior: "instant" });
  }, [seriesId]);
  const series = seriesQuery.data;
  // The background document is written for parsing models, not viewers; only managers see it.
  const hasParseNotes = Boolean(series && (readBackground(series.importantExtraData).content.trim() || series.characterBible.length));
  const episodes = [...(series?.videos ?? [])].sort((a, b) => a.episodeOrder - b.episodeOrder || a.uploadSequence - b.uploadSequence);
  const playable = episodes.filter((video) => video.playbackStatus === "READY" && video.playbackUrl);
  const history = watchHistory.filter((item) => item.seriesId === seriesId);
  const recent = history.find((item) => playable.some((video) => video.videoId === item.videoId));
  const featured = playable.find((video) => video.videoId === recent?.videoId) ?? playable[0];
  const position = featured && recent ? Math.min(recent.positionSeconds, featured.durationSeconds) : 0;
  const framePosition = recent ? position : Math.min(30_000, (featured?.durationSeconds ?? 0) * 0.1);

  return <main ref={pageRef} className={`page ${styles.page}`}>
    {!seriesId ? <p className={styles.state} role="alert"><Mascot mood="puzzled" size={64} />系列地址无效。</p>
      : seriesQuery.isPending ? <p className={styles.state} role="status"><Mascot mood="seek" size={64} />正在读取视频系列…</p>
      : seriesQuery.isError ? <div className={styles.state}><Mascot mood="oops" size={64} /><p role="alert">读取视频系列失败：{errorMessage(seriesQuery.error)}</p><button className="button-secondary" type="button" onClick={() => void seriesQuery.refetch()}>重试</button><Link to="/watch">返回首页</Link></div>
      : series ? <>
        <section className={styles.hero} aria-label="系列介绍">
          <PlaybackFrame className={styles.heroFrame} eager src={mediaUrl(featured?.playbackUrl ?? null)} positionSeconds={framePosition}
            poster={mediaUrl(series.cover?.url ?? featured?.thumbnail?.url ?? null)} />
          <Link className={styles.back} to="/watch"><ArrowLeft size={17} aria-hidden="true" />全部系列</Link>
          <div className={styles.heroContent}>
            <span className={styles.eyebrow}>视频系列</span>
            <h1>{seriesTitle(series)}</h1>
            <div className={styles.meta}><span>共 {episodes.length} 集</span>{featured ? <span>{recent ? "上次观看" : "从这里开始"} · {episodeTitle(featured.episodeOrder)}</span> : null}</div>
            <div className={styles.actions}>
              {featured ? <Link className={styles.play} to={`/watch/${encodeURIComponent(series.seriesId)}/${encodeURIComponent(featured.videoId)}`}>
                <Play size={21} fill="currentColor" aria-hidden="true" />{recent ? "继续观看" : "开始观看"}
              </Link> : <span className={styles.unavailable}>暂无可播放的剧集</span>}
              {episodes.length > 0 ? <a className={styles.browse} href="#series-episodes">查看剧集<ChevronDown size={18} aria-hidden="true" /></a> : null}
            </div>
            {featured && recent ? <div className={styles.progress}>
              <span>已看至 {formatTimecode(position)} / {formatTimecode(featured.durationSeconds)}</span>
              {featured.durationSeconds > 0 ? <progress aria-label="上次观看进度" value={position} max={featured.durationSeconds} /> : null}
            </div> : null}
          </div>
        </section>
        <section className={styles.overview} aria-label="系列简介">
          <h2>关于这部作品</h2>
          <div className={styles.overviewContent}>
            <p>{series.description?.trim() || "还没有填写系列简介。"}</p>
            {canManage && hasParseNotes ? <details className={styles.parseNotes}>
              <summary><span>解析用人物与背景资料</span><small>仅管理者可见 · 供模型解析参考</small><ChevronDown size={16} aria-hidden="true" /></summary>
              <SeriesBackgroundEditor key={series.seriesId} series={series} readOnly />
            </details> : null}
          </div>
        </section>
        <section id="series-episodes" className={styles.episodes} aria-label="系列选集">
          <header><h2>剧集 <small>{episodes.length} 集</small></h2><span>按集数顺序</span></header>
          {episodes.length === 0 ? <p className={styles.empty}>这个系列还没有视频。</p> : <ol className={styles.episodeGrid}>
            {episodes.map((episode) => {
              const available = episode.playbackStatus === "READY" && Boolean(episode.playbackUrl);
              const saved = history.find((item) => item.videoId === episode.videoId);
              const watched = Math.min(saved?.positionSeconds ?? 0, episode.durationSeconds);
              const content = <>
                <span className={styles.preview} aria-hidden="true">
                  <Clapperboard size={32} strokeWidth={1} />
                  {episode.thumbnail ? <img src={mediaUrl(episode.thumbnail.url) ?? undefined} alt="" loading="lazy" onError={(event) => { event.currentTarget.hidden = true; }} />
                    : available ? <PlaybackFrame className={styles.episodeFrame} src={mediaUrl(episode.playbackUrl)} positionSeconds={episode.durationSeconds * 0.1} /> : null}
                  {available ? <span className={styles.previewPlay}><Play size={24} fill="currentColor" /></span> : null}
                  <span className={styles.episodeNumber}>{episodeLabel(episode.episodeOrder)}</span>
                  <span className={styles.duration}>{formatTimecode(episode.durationSeconds)}</span>
                  {watched > 0 && episode.durationSeconds > 0 ? <span className={styles.watched} style={{ width: `${watched / episode.durationSeconds * 100}%` }} /> : null}
                </span>
                <strong title={episode.originalFilename}>{episodeTitle(episode.episodeOrder)}</strong>
                <small>{available ? watched > 0 ? `已看至 ${formatTimecode(watched)}` : "开始观看" : "暂不可播放"}</small>
              </>;
              return <li key={episode.videoId}>{available
                ? <Link className={styles.episode} to={`/watch/${encodeURIComponent(series.seriesId)}/${encodeURIComponent(episode.videoId)}`}>{content}</Link>
                : <div className={styles.episode} aria-disabled="true">{content}</div>}</li>;
            })}
          </ol>}
        </section>
      </> : <p className={styles.state} role="alert"><Mascot mood="puzzled" size={64} />视频系列不存在。</p>}
  </main>;
}
