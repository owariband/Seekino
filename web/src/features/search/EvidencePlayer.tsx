import { Captions, CircleAlert, Clock3, Play, ScanSearch } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { errorMessage, videoContentUrl } from "../../api/client";
import type { VideoChunkSearchHit } from "../../api/contracts";
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

  useEffect(() => {
    setTab("reason");
    setPlayError(null);
    const video = videoRef.current;
    if (!video || !hit) return;
    const start = hit.evidence.startMs / 1000;
    const end = hit.evidence.endMs / 1000;
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
  }, [hit?.evidence.workspaceId, hit?.evidence.videoId, hit?.evidence.videoChunkId, hit?.evidence.startMs, hit?.evidence.endMs]);

  if (!hit) {
    return (
      <aside className={styles.evidencePanel}>
        <div className={styles.evidenceEmpty}>
          <ScanSearch size={26} strokeWidth={1.4} />
          <h2>证据会在这里展开</h2>
          <p>从 Agent 召回的候选中选择一段，即可核验画面、对白和前后语境。</p>
        </div>
      </aside>
    );
  }

  const { evidence } = hit;
  const explanation = explainHit(hit);

  return (
    <aside className={styles.evidencePanel}>
      <div className={styles.viewer}>
        <video key={`${evidence.workspaceId}:${evidence.videoId}`} ref={videoRef} controls preload="metadata" src={videoContentUrl(evidence.videoId, evidence.workspaceId)} aria-label="视频证据" />
        <div className={styles.frameMeta}>
          <span>{episodeLabel(evidence.episodeOrder)}</span>
          <strong>{formatTimeRange(evidence.startMs, evidence.endMs)}</strong>
        </div>
      </div>

      <div className={styles.playerHeading}>
        <div>
          <span className="eyebrow">Evidence window · 证据窗口</span>
          <h2>{evidence.seriesName}</h2>
        </div>
        <button className="button-secondary" type="button" onClick={() => playRef.current?.()}>
          <Play size={15} fill="currentColor" />
          从 {formatTimecode(evidence.startMs)} 播放
        </button>
      </div>

      {playError ? <p className={styles.messageError} role="alert">{playError}</p> : null}

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
              <span className={styles.reasonNumber}>{String(hit.candidate.fusedRank).padStart(2, "0")}</span>
              <div>
                <span className="eyebrow">Agent’s selection note</span>
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

      <p className={styles.explainDisclosure}>理由由返回的对白、画面事件、排名和上下文整理而成，可逐项核验；不声称展示模型的私有思考过程。</p>
    </aside>
  );
}
