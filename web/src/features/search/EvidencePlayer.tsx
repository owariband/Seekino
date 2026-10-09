import { Captions, CircleAlert, Clock3, Pause, Play, ScanSearch } from "lucide-react";
import { type CSSProperties, type KeyboardEvent, useEffect, useRef, useState } from "react";

import { errorMessage, getVideoChunk, mediaUrl } from "../../api/client";
import type { VideoChunkEvidence, VideoChunkSearchHit } from "../../api/contracts";
import { episodeLabel, formatTimeRange, formatTimecode } from "../../shared/format";
import { explainHit, readableTranscript } from "./explain";
import styles from "./search.module.css";

type EvidenceTab = "reason" | "evidence" | "context";

interface EvidencePlayerProps {
  hit: VideoChunkSearchHit | null;
  /** Integer milliseconds; range field names remain unchanged. */
  playbackRange: { startSeconds: number; endSeconds: number };
}

export function EvidencePlayer({ hit, playbackRange }: EvidencePlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const playRef = useRef<(() => void) | null>(null);
  const playingRef = useRef(false);
  const [tab, setTab] = useState<EvidenceTab>("reason");
  const [playError, setPlayError] = useState<string | null>(null);
  const [mediaReady, setMediaReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [relativePositionSeconds, setRelativePositionSeconds] = useState(0);
  const [refreshed, setRefreshed] = useState<VideoChunkEvidence | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [failedImage, setFailedImage] = useState<string | null>(null);
  const evidence = refreshed?.videoChunkId === hit?.evidence.videoChunkId && refreshed?.tenantId === hit?.evidence.tenantId ? refreshed : hit?.evidence;
  const selectionRef = useRef<string | null>(null);
  const selectionKey = evidence ? `${evidence.tenantId}:${evidence.videoChunkId}` : null;
  selectionRef.current = selectionKey;
  useEffect(() => { setRefreshing(false); }, [hit?.evidence.tenantId, hit?.evidence.videoChunkId]);
  const playbackUrl = evidence?.playbackStatus === "READY" ? mediaUrl(evidence.playbackUrl) : null;
  const clipDurationSeconds = Math.max(0, playbackRange.endSeconds - playbackRange.startSeconds);
  const playbackKey = evidence && playbackUrl ? `${evidence.tenantId}:${evidence.videoId}:${evidence.videoChunkId}:${playbackRange.startSeconds}:${playbackRange.endSeconds}:${playbackUrl}` : null;

  useEffect(() => {
    setTab("reason");
    setPlayError(null);
    setMediaReady(false);
    setPlaying(false);
    setRelativePositionSeconds(0);
    playingRef.current = false;
    const video = videoRef.current;
    if (!video || !evidence || !playbackUrl) return;
    // HTML media APIs use seconds; all stored and API ranges use integer milliseconds.
    const start = playbackRange.startSeconds / 1000;
    const end = playbackRange.endSeconds / 1000;
    let active = true;
    let requested = false;
    video.pause();
    const startPlayback = () => {
      requested = false;
      void video.play().catch((error: unknown) => {
        if (active && !(error instanceof DOMException && error.name === "AbortError")) setPlayError(errorMessage(error));
      });
    };
    const positionAtStart = () => {
      video.currentTime = start;
      setRelativePositionSeconds(0);
    };
    const prepare = () => {
      setMediaReady(true);
      positionAtStart();
      if (requested) startPlayback();
    };
    const syncPosition = () => {
      if (video.currentTime < start) {
        positionAtStart();
        return;
      }
      if (video.currentTime >= end) {
        video.pause();
        video.currentTime = end;
        setRelativePositionSeconds(clipDurationSeconds);
        playingRef.current = false;
        setPlaying(false);
        return;
      }
      setRelativePositionSeconds(Math.min(clipDurationSeconds, Math.max(0, Math.round(video.currentTime * 1000) - playbackRange.startSeconds)));
    };
    const keepInRange = () => {
      if (video.currentTime < start || video.currentTime >= end) positionAtStart();
      playingRef.current = true;
      setPlaying(true);
    };
    const markPaused = () => {
      playingRef.current = false;
      setPlaying(false);
    };
    playRef.current = () => {
      setPlayError(null);
      requested = true;
      if (video.readyState >= HTMLMediaElement.HAVE_METADATA) {
        if (video.currentTime < start || video.currentTime >= end) positionAtStart();
        startPlayback();
      }
    };
    video.addEventListener("loadedmetadata", prepare);
    video.addEventListener("timeupdate", syncPosition);
    video.addEventListener("play", keepInRange);
    video.addEventListener("pause", markPaused);
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) prepare();
    return () => {
      active = false;
      requested = false;
      playRef.current = null;
      video.removeEventListener("loadedmetadata", prepare);
      video.removeEventListener("timeupdate", syncPosition);
      video.removeEventListener("play", keepInRange);
      video.removeEventListener("pause", markPaused);
      video.pause();
    };
  }, [clipDurationSeconds, evidence?.tenantId, evidence?.videoId, evidence?.videoChunkId, playbackRange.endSeconds, playbackRange.startSeconds, playbackUrl]);

  if (!hit || !evidence) {
    return (
      <aside className={styles.evidencePanel}>
        <div className={styles.evidenceEmpty}>
          <ScanSearch size={26} strokeWidth={1.4} />
          <p>选择片段，查看画面与证据。</p>
        </div>
      </aside>
    );
  }

  const explanation = explainHit(hit);
  const seekClip = (relativeSeconds: number) => {
    const video = videoRef.current;
    if (!video || !mediaReady) return;
    const nextPositionSeconds = Math.min(clipDurationSeconds, Math.max(0, relativeSeconds));
    video.currentTime = (playbackRange.startSeconds + nextPositionSeconds) / 1000;
    setRelativePositionSeconds(nextPositionSeconds);
    if (nextPositionSeconds >= clipDurationSeconds) video.pause();
  };
  const seekClipWithKeyboard = (event: KeyboardEvent<HTMLInputElement>) => {
    let nextPositionSeconds: number;
    switch (event.key) {
      case "ArrowLeft":
      case "ArrowDown": nextPositionSeconds = relativePositionSeconds - 1000; break;
      case "ArrowRight":
      case "ArrowUp": nextPositionSeconds = relativePositionSeconds + 1000; break;
      case "PageDown": nextPositionSeconds = relativePositionSeconds - 5000; break;
      case "PageUp": nextPositionSeconds = relativePositionSeconds + 5000; break;
      case "Home": nextPositionSeconds = 0; break;
      case "End": nextPositionSeconds = clipDurationSeconds; break;
      default: return;
    }
    event.preventDefault();
    seekClip(nextPositionSeconds);
  };
  const refreshMedia = async () => {
    setRefreshing(true);
    try {
      const next = await getVideoChunk(evidence.videoChunkId, evidence.tenantId);
      if (selectionRef.current !== selectionKey) return;
      if (next.videoChunkId !== evidence.videoChunkId || next.tenantId !== evidence.tenantId) throw new Error("返回的视频证据不属于当前片段");
      setRefreshed(next);
      setPlayError(null);
    } catch (error) { if (selectionRef.current === selectionKey) setPlayError(errorMessage(error)); }
    finally { if (selectionRef.current === selectionKey) setRefreshing(false); }
  };

  return (
    <aside className={styles.evidencePanel}>
      <div className={styles.viewer}>
        {playbackUrl ? <video key={playbackKey} ref={videoRef} playsInline preload="metadata" src={playbackUrl} poster={evidence.thumbnail && failedImage !== evidence.thumbnail.imageId ? mediaUrl(evidence.thumbnail.url) ?? undefined : undefined} onError={() => setPlayError("视频媒体无法读取，请刷新媒体状态后重试。")} aria-label="视频证据" />
          : <p role="status">{evidence.playbackStatus === "MISSING" ? "原视频文件已丢失，仍可查看文字证据。" : evidence.playbackStatus === "PREPARING" ? "播放媒体正在准备，仍可查看文字证据。" : "暂无可播放媒体，仍可查看文字证据。"}</p>}
        {playbackUrl ? (
          <div className={styles.clipControls}>
            <button
              className={styles.clipToggle}
              type="button"
              onClick={() => {
                if (playingRef.current) videoRef.current?.pause();
                else playRef.current?.();
              }}
              aria-label={playing ? "暂停视频片段" : "播放视频片段"}
            >
              {playing ? <Pause size={18} fill="currentColor" aria-hidden="true" /> : <Play size={18} fill="currentColor" aria-hidden="true" />}
            </button>
            <input
              className={styles.clipTimeline}
              type="range"
              min={0}
              max={clipDurationSeconds}
              step={1}
              value={Math.min(relativePositionSeconds, clipDurationSeconds)}
              disabled={!mediaReady || clipDurationSeconds <= 0}
              onChange={(event) => seekClip(Number(event.currentTarget.value))}
              onKeyDown={seekClipWithKeyboard}
              aria-label="视频片段播放进度"
              aria-valuetext={`${formatTimecode(relativePositionSeconds)} / ${formatTimecode(clipDurationSeconds)}`}
              style={{ "--clip-progress": `${clipDurationSeconds > 0 ? (relativePositionSeconds / clipDurationSeconds) * 100 : 0}%` } as CSSProperties}
            />
            <time className={styles.clipTime}>{formatTimecode(relativePositionSeconds)} / {formatTimecode(clipDurationSeconds)}</time>
          </div>
        ) : null}
        <div className={styles.frameMeta}>
          <span>{episodeLabel(evidence.episodeOrder)}</span>
          <strong>{formatTimeRange(playbackRange.startSeconds, playbackRange.endSeconds)}</strong>
        </div>
      </div>

      <div className={styles.playerHeading}>
        <div>
          <h2>{evidence.seriesName}</h2>
          <p>片段范围 {formatTimeRange(playbackRange.startSeconds, playbackRange.endSeconds)}</p>
        </div>
        {playbackUrl ? <a className="button-quiet" href={`/watch/${encodeURIComponent(evidence.seriesId)}/${encodeURIComponent(evidence.videoId)}?atSeconds=${playbackRange.startSeconds}`}>在整集中观看</a> : null}
      </div>

      {playError ? <p className={styles.messageError} role="alert">{playError}</p> : null}
      {!playbackUrl || playError ? <button className="button-secondary" type="button" disabled={refreshing} onClick={() => void refreshMedia()}>{refreshing ? "正在刷新媒体状态…" : "刷新媒体状态"}</button> : null}
      {!playbackUrl && evidence.thumbnail ? failedImage === evidence.thumbnail.imageId ? <p>关键帧图片无法读取</p> : <img src={mediaUrl(evidence.thumbnail.url) ?? undefined} alt={evidence.thumbnail.alt ?? "已登记关键帧"} onError={() => setFailedImage(evidence.thumbnail!.imageId)} /> : null}

      <nav className={styles.evidenceTabs} aria-label="证据详情">
        {([
          ["reason", "选择理由"],
          ["evidence", "原始证据"],
          ["context", "前后语境"],
        ] as const).map(([value, label]) => (
          <button
            type="button"
            key={value}
            className={tab === value ? styles.activeTab : undefined}
            onClick={() => setTab(value)}
          >
            {label}
          </button>
        ))}
      </nav>

      <div className={styles.evidenceContent}>
        {tab === "reason" ? (
          <>
            <section className={styles.reasonLead}>
              <span className={styles.reasonNumber}>{String(hit.candidate.rank).padStart(2, "0")}</span>
              <div>
                <p>{explanation.summary}</p>
              </div>
            </section>
            <section className={styles.factSection}>
              <h3>命中的可核验事实</h3>
              <ul>
                {explanation.matchedFacts.map((fact) => <li key={fact}>{fact}</li>)}
              </ul>
            </section>
            <section className={styles.sourceGrid}>
              {explanation.sources.map((source) => (
                <div key={source}><ScanSearch size={14} /><span>{source}</span></div>
              ))}
            </section>
            {explanation.limitations.length > 0 ? (
              <section className={styles.limitations}>
                <CircleAlert size={16} />
                <div>
                  <strong>核验时留意</strong>
                  {explanation.limitations.map((item) => <p key={item}>{item}</p>)}
                </div>
              </section>
            ) : null}
          </>
        ) : null}

        {tab === "evidence" ? (
          <div className={styles.quoteStack}>
            <section>
              <Captions size={17} />
              <div><span>对白 / Transcript</span><blockquote>{readableTranscript(hit)}</blockquote></div>
            </section>
            <section>
              <ScanSearch size={17} />
              <div><span>画面 / Visual event</span><p>{evidence.baselineVisualCaption || evidence.allTimeEventText || "暂无画面描述"}</p></div>
            </section>
          </div>
        ) : null}

        {tab === "context" ? (
          <div className={styles.contextFlow}>
            <article><span>BEFORE</span><p>{evidence.contextBeforeCompact || "暂无前文摘要"}</p></article>
            <i />
            <article className={styles.contextCurrent}><span>MATCH</span><p>{evidence.videoCompact || evidence.windowSummary}</p></article>
            <i />
            <article><span>AFTER</span><p>{evidence.contextAfterCompact || "暂无后文摘要"}</p></article>
            <div className={styles.windowTime}><Clock3 size={14} />分析窗口 {formatTimeRange(evidence.analysisWindowStartSeconds, evidence.analysisWindowEndSeconds)}</div>
          </div>
        ) : null}
      </div>

    </aside>
  );
}
