import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ChevronDown, Clapperboard, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { errorMessage, getSeries, mediaUrl, shouldRetryApiRequest, TENANT_ID } from "../../api/client";
import { episodeLabel, episodeTitle, formatTimecode, previewFramePosition, seriesTitle } from "../../shared/format";
import { Mascot } from "../../shared/Mascot";
import { readBackground, SeriesBackgroundEditor } from "../library/SeriesBackgroundEditor";
import { SeriesActionsMenu } from "../library/SeriesActionsMenu";
import { watchPath } from "../library/seriesLinks";
import { PlaybackFrame } from "./PlaybackFrame";
import { readWatchHistory } from "./playbackProgress";
import { useI18n } from "../../app/i18n";
import styles from "./series.module.css";

export function SeriesPage() {
  const { t, locale } = useI18n();
  const { seriesId } = useParams();
  const [searchParams] = useSearchParams();
  const tenantId = searchParams.get("tenantId") || TENANT_ID;
  const pageRef = useRef<HTMLElement>(null);
  const [watchHistory, setWatchHistory] = useState(() => readWatchHistory());
  const seriesQuery = useQuery({
    queryKey: ["series-detail", seriesId, tenantId], queryFn: () => getSeries(seriesId!, tenantId),
    enabled: Boolean(seriesId), retry: shouldRetryApiRequest,
  });
  useEffect(() => {
    // The departing player saves its final position during effect cleanup.
    setWatchHistory(readWatchHistory());
    pageRef.current?.scrollIntoView?.({ block: "start", behavior: "instant" });
  }, [seriesId]);
  const series = seriesQuery.data;
  const canManage = series?.access === "member";
  // The background document is written for parsing models, not viewers; only managers see it.
  const hasParseNotes = Boolean(series && (readBackground(series.importantExtraData).content.trim() || series.characterBible.length));
  const episodes = [...(series?.videos ?? [])].sort((a, b) => a.episodeOrder - b.episodeOrder || a.uploadSequence - b.uploadSequence);
  const playable = episodes.filter((video) => video.playbackStatus === "READY" && video.playbackUrl);
  const history = watchHistory.filter((item) => item.seriesId === seriesId);
  const recent = history.find((item) => playable.some((video) => video.videoId === item.videoId));
  const featured = playable.find((video) => video.videoId === recent?.videoId) ?? playable[0];
  const position = featured && recent ? Math.min(recent.positionSeconds, featured.durationSeconds) : 0;
  const framePosition = recent ? position : Math.min(30_000, (featured?.durationSeconds ?? 0) * 0.1);
  const description = series?.description?.trim();

  return <main ref={pageRef} className={`page ${styles.page}`}>
    {!seriesId ? <p className={styles.state} role="alert"><Mascot mood="puzzled" size={64} />{t("系列地址无效。")}</p>
      : seriesQuery.isPending ? <p className={styles.state} role="status"><Mascot mood="seek" size={64} delayed />{t("正在读取视频系列…")}</p>
      : seriesQuery.isError ? <div className={styles.state}><Mascot mood="oops" size={64} /><p role="alert">{t("读取视频系列失败：{message}", { message: errorMessage(seriesQuery.error, locale) })}</p><button className="button-secondary" type="button" onClick={() => void seriesQuery.refetch()}>{t("重试")}</button><Link to="/watch">{t("返回首页")}</Link></div>
      : series ? <>
        <section className={styles.hero} aria-label={t("系列介绍")}>
          <PlaybackFrame className={styles.heroFrame} eager src={mediaUrl(featured?.playbackUrl ?? null)} positionSeconds={framePosition}
            poster={mediaUrl(series.cover?.url ?? featured?.thumbnail?.url ?? null)} />
          <div className={styles.heroToolbar}>
            <Link className={styles.back} to="/watch"><ArrowLeft size={17} aria-hidden="true" />{t("全部系列")}</Link>
            <SeriesActionsMenu series={series} />
          </div>
          <div className={styles.heroContent}>
            <span className={styles.eyebrow}>{t("视频系列")}</span>
            <h1>{seriesTitle(series)}</h1>
            <div className={styles.meta}><span>{t("共 {count} 集", { count: episodes.length })}</span>{featured ? <span>{recent ? t("上次观看") : t("从这里开始")} · {episodeTitle(featured.episodeOrder, locale)}</span> : null}</div>
            {series.visibility === "public" && series.publisher ? <div className={styles.publisher}>
              <span className={styles.publisherAvatar} aria-hidden="true">{series.publisher.displayName.slice(0, 1).toUpperCase()}
                {series.publisher.avatar ? <img src={mediaUrl(series.publisher.avatar.url) ?? undefined} alt="" onError={(event) => { event.currentTarget.hidden = true; }} /> : null}
              </span>
              <span>{t("{name} 分享", { name: series.publisher.displayName })}</span>
            </div> : null}
            <div className={styles.actions}>
              {featured ? <Link className={styles.play} to={watchPath(series, featured.videoId)}>
                <Play size={21} fill="currentColor" aria-hidden="true" />{recent ? t("继续观看") : t("开始观看")}
              </Link> : <span className={styles.unavailable}>{t("暂无可播放的剧集")}</span>}
              {episodes.length > 0 ? <a className={styles.browse} href="#series-episodes">{t("查看剧集")}<ChevronDown size={18} aria-hidden="true" /></a> : null}
            </div>
            {featured && recent ? <div className={styles.progress}>
              <span>{t("已看至 {time} / {duration}", { time: formatTimecode(position), duration: formatTimecode(featured.durationSeconds) })}</span>
              {featured.durationSeconds > 0 ? <progress aria-label={t("上次观看进度")} value={position} max={featured.durationSeconds} /> : null}
            </div> : null}
          </div>
        </section>
        {/* A missing description is a to-do for managers; viewers simply skip the section. */}
        {description || canManage ? <section className={styles.overview} aria-label={t("系列简介")}>
          <h2>{t("关于这部作品")}</h2>
          <div className={styles.overviewContent}>
            <p className={description ? undefined : styles.placeholder}>{description || t("还没有填写系列简介。")}</p>
            {canManage && hasParseNotes ? <details className={styles.parseNotes}>
              <summary><span>{t("解析用人物与背景资料")}</span><small>{t("仅管理者可见 · 供模型解析参考")}</small><ChevronDown size={16} aria-hidden="true" /></summary>
              <SeriesBackgroundEditor key={series.seriesId} series={series} readOnly />
            </details> : null}
          </div>
        </section> : null}
        <section id="series-episodes" className={styles.episodes} aria-label={t("系列选集")}>
          <header><h2>{t("剧集")} <small>{t("{count} 集", { count: episodes.length })}</small></h2><span>{t("按集数顺序")}</span></header>
          {episodes.length === 0 ? <p className={styles.empty}>{t("这个系列还没有视频。")}</p> : <ol className={styles.episodeGrid}>
            {episodes.map((episode) => {
              const available = episode.playbackStatus === "READY" && Boolean(episode.playbackUrl);
              const saved = history.find((item) => item.videoId === episode.videoId);
              const watched = Math.min(saved?.positionSeconds ?? 0, episode.durationSeconds);
              const content = <>
                <span className={styles.preview} aria-hidden="true">
                  <Clapperboard size={32} strokeWidth={1} />
                  {episode.thumbnail ? <img src={mediaUrl(episode.thumbnail.url) ?? undefined} alt="" loading="lazy" onError={(event) => { event.currentTarget.hidden = true; }} />
                    : available ? <PlaybackFrame className={styles.episodeFrame} src={mediaUrl(episode.playbackUrl)} positionSeconds={previewFramePosition(episode.durationSeconds)} /> : null}
                  {available ? <span className={styles.previewPlay}><Play size={24} fill="currentColor" /></span> : null}
                  <span className={styles.episodeNumber}>{episodeLabel(episode.episodeOrder)}</span>
                  <span className={styles.duration}>{formatTimecode(episode.durationSeconds)}</span>
                  {watched > 0 && episode.durationSeconds > 0 ? <span className={styles.watched} style={{ width: `${watched / episode.durationSeconds * 100}%` }} /> : null}
                </span>
                <strong title={episode.originalFilename ?? undefined}>{episodeTitle(episode.episodeOrder, locale)}</strong>
                {!available ? <small>{t("暂不可播放")}</small> : watched > 0 ? <small>{t("已看至 {time}", { time: formatTimecode(watched) })}</small> : null}
              </>;
              return <li key={episode.videoId}>{available
                ? <Link className={styles.episode} to={watchPath(series, episode.videoId)}>{content}</Link>
                : <div className={styles.episode} aria-disabled="true">{content}</div>}</li>;
            })}
          </ol>}
        </section>
      </> : <p className={styles.state} role="alert"><Mascot mood="puzzled" size={64} />{t("视频系列不存在。")}</p>}
  </main>;
}
