import { Captions, CircleAlert, Clock3, Play, ScanSearch } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { errorMessage, getVideoChunk, mediaUrl } from "../../api/client";
import type { VideoChunkEvidence, VideoChunkSearchHit } from "../../api/contracts";
import { episodeLabel, formatTimeRange, formatTimecode } from "../../shared/format";
import { explainHit, readableTranscript } from "./explain";
import styles from "./search.module.css";

type EvidenceTab = "reason" | "evidence" | "context";

interface EvidencePlayerProps {
  hit: VideoChunkSearchHit | null;
}

export function EvidencePlayer({ hit }: EvidencePlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const playRef = useRef<(() => void) | null>(null);
  const [tab, setTab] = useState<EvidenceTab>("reason");
  const [playError, setPlayError] = useState<string | null>(null);
  const [refreshed, setRefreshed] = useState<VideoChunkEvidence | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [failedImage, setFailedImage] = useState<string | null>(null);
  const evidence = refreshed?.videoChunkId === hit?.evidence.videoChunkId && refreshed?.workspaceId === hit?.evidence.workspaceId ? refreshed : hit?.evidence;
  const selectionRef = useRef<string | null>(null);
  const selectionKey = evidence ? `${evidence.workspaceId}:${evidence.videoChunkId}` : null;
  selectionRef.current = selectionKey;
  useEffect(() => { setRefreshing(false); }, [hit?.evidence.workspaceId, hit?.evidence.videoChunkId]);
  const playbackUrl = evidence?.playbackStatus === "READY" ? mediaUrl(evidence.playbackUrl) : null;

  useEffect(() => {
    setTab("reason");
    setPlayError(null);
    const video = videoRef.current;
    if (!video || !evidence || !playbackUrl) return;
    const start = evidence.startMs / 1000;
    const end = evidence.endMs / 1000;
    let active = true;
    let requested = false;
    video.pause();
    const position = () => {
      video.currentTime = start;
      if (requested) {
        requested = false;
        void video.play().catch((error: unknown) => {
          if (active) setPlayError(errorMessage(error));
        });
      }
    };
    const stopAtEnd = () => {
      if (video.currentTime >= end) {
        video.pause();
        video.currentTime = end;
      }
    };
    const keepInRange = () => {
      if (video.currentTime < start || video.currentTime >= end) video.currentTime = start;
    };
    playRef.current = () => {
      setPlayError(null);
      requested = true;
      if (video.readyState >= HTMLMediaElement.HAVE_METADATA) position();
    };
    video.addEventListener("loadedmetadata", position);
    video.addEventListener("timeupdate", stopAtEnd);
    video.addEventListener("play", keepInRange);
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) position();
    return () => {
      active = false;
      requested = false;
      playRef.current = null;
      video.removeEventListener("loadedmetadata", position);
      video.removeEventListener("timeupdate", stopAtEnd);
      video.removeEventListener("play", keepInRange);
      video.pause();
    };
  }, [evidence?.workspaceId, evidence?.videoId, evidence?.videoChunkId, evidence?.startMs, evidence?.endMs, playbackUrl]);

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
  const refreshMedia = async () => {
    setRefreshing(true);
    try {
      const next = await getVideoChunk(evidence.videoChunkId, evidence.workspaceId);
      if (selectionRef.current !== selectionKey) return;
      if (next.videoChunkId !== evidence.videoChunkId || next.workspaceId !== evidence.workspaceId) throw new Error("返回的视频证据不属于当前片段");
      setRefreshed(next);
      setPlayError(null);
    } catch (error) { if (selectionRef.current === selectionKey) setPlayError(errorMessage(error)); }
    finally { if (selectionRef.current === selectionKey) setRefreshing(false); }
  };

  return (
    <aside className={styles.evidencePanel}>
      <div className={styles.viewer}>
        {playbackUrl ? <video key={`${evidence.workspaceId}:${evidence.videoId}:${evidence.videoChunkId}:${playbackUrl}`} ref={videoRef} controls preload="metadata" src={playbackUrl} poster={evidence.thumbnail && failedImage !== evidence.thumbnail.imageId ? mediaUrl(evidence.thumbnail.url) ?? undefined : undefined} onError={() => setPlayError("视频媒体无法读取，请刷新媒体状态后重试。")} aria-label="视频证据" />
          : <p role="status">{evidence.playbackStatus === "MISSING" ? "原视频文件已丢失，仍可查看文字证据。" : evidence.playbackStatus === "PREPARING" ? "播放媒体正在准备，仍可查看文字证据。" : "暂无可播放媒体，仍可查看文字证据。"}</p>}
        <div className={styles.frameMeta}>
          <span>{episodeLabel(evidence.episodeOrder)}</span>
          <strong>{formatTimeRange(evidence.startMs, evidence.endMs)}</strong>
        </div>
      </div>

      <div className={styles.playerHeading}>
        <div>
          <h2>{evidence.seriesName}</h2>
        </div>
        <button className="button-secondary" type="button" disabled={!playbackUrl} onClick={() => playRef.current?.()}>
          <Play size={15} fill="currentColor" />
          从 {formatTimecode(evidence.startMs)} 播放
        </button>
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
            <div className={styles.windowTime}><Clock3 size={14} />分析窗口 {formatTimeRange(evidence.analysisWindowStartMs, evidence.analysisWindowEndMs)}</div>
          </div>
        ) : null}
      </div>

    </aside>
  );
}
