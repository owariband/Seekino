import { Maximize, Minimize, Pause, Play, RotateCcw, RotateCw, Volume2, VolumeX } from "lucide-react";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";

import { errorMessage, mediaUrl } from "../../api/client";
import type { VideoSummary } from "../../api/contracts";
import { episodeLabel, formatTimecode } from "../../shared/format";
import { readVideoProgress, saveVideoProgress } from "./playbackProgress";
import styles from "./watch.module.css";

interface VideoPlayerProps {
  video: VideoSummary;
  atMs?: number;
  fullscreen?: boolean;
  onToggleFullscreen?: () => void | Promise<void>;
}

const PROGRESS_WRITE_INTERVAL_MS = 5_000;

export function VideoPlayer({ video, atMs, fullscreen = false, onToggleFullscreen }: VideoPlayerProps) {
  const mediaRef = useRef<HTMLVideoElement>(null);
  const positionRef = useRef(0);
  const durationRef = useRef(video.durationMs);
  const progressReadyRef = useRef(false);
  const lastWriteAtRef = useRef(0);
  const initializedKeyRef = useRef<string | null>(null);
  const [currentMs, setCurrentMs] = useState(0);
  const [durationMs, setDurationMs] = useState(video.durationMs);
  const [mediaReady, setMediaReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [playError, setPlayError] = useState<string | null>(null);
  const playbackUrl = video.playbackStatus === "READY" ? mediaUrl(video.playbackUrl) : null;
  const mediaKey = `${video.videoId}:${atMs ?? "history"}:${playbackUrl ?? "none"}`;

  useEffect(() => {
    positionRef.current = 0;
    durationRef.current = video.durationMs;
    progressReadyRef.current = false;
    lastWriteAtRef.current = 0;
    initializedKeyRef.current = null;
    setCurrentMs(0);
    setDurationMs(video.durationMs);
    setMediaReady(false);
    setPlaying(false);
    setVolume(1);
    setMuted(false);
    setPlaybackRate(1);
    setPlayError(null);
    return () => {
      if (progressReadyRef.current) {
        saveVideoProgress({ seriesId: video.seriesId, videoId: video.videoId, positionMs: positionRef.current, durationMs: durationRef.current });
      }
    };
  }, [video.seriesId, video.videoId, mediaKey, video.durationMs]);

  useEffect(() => {
    const flush = () => {
      if (progressReadyRef.current) {
        saveVideoProgress({ seriesId: video.seriesId, videoId: video.videoId, positionMs: positionRef.current, durationMs: durationRef.current });
      }
    };
    const flushWhenHidden = () => { if (document.visibilityState === "hidden") flush(); };
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", flushWhenHidden);
    return () => {
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", flushWhenHidden);
    };
  }, [video.seriesId, video.videoId]);

  const persist = () => {
    if (!progressReadyRef.current) return;
    saveVideoProgress({ seriesId: video.seriesId, videoId: video.videoId, positionMs: positionRef.current, durationMs: durationRef.current });
    lastWriteAtRef.current = Date.now();
  };

  const syncPosition = () => {
    const media = mediaRef.current;
    if (!media) return;
    const next = Math.max(0, Math.round(media.currentTime * 1000));
    positionRef.current = next;
    setCurrentMs(next);
    if (Date.now() - lastWriteAtRef.current >= PROGRESS_WRITE_INTERVAL_MS) persist();
  };

  const onLoadedMetadata = () => {
    const media = mediaRef.current;
    if (!media || initializedKeyRef.current === mediaKey) return;
    const knownDuration = Number.isFinite(media.duration) && media.duration > 0 ? Math.round(media.duration * 1000) : video.durationMs;
    durationRef.current = knownDuration;
    setDurationMs(knownDuration);
    const saved = readVideoProgress(video.videoId);
    const resume = saved?.seriesId === video.seriesId ? saved.positionMs : 0;
    const nearEnd = knownDuration > 0 && resume >= knownDuration - Math.min(15_000, knownDuration * 0.05);
    const requested = atMs !== undefined && Number.isSafeInteger(atMs) && atMs >= 0 ? atMs : nearEnd ? 0 : resume;
    const startMs = Math.min(knownDuration, Math.max(0, requested));
    media.currentTime = startMs / 1000;
    positionRef.current = startMs;
    setCurrentMs(startMs);
    progressReadyRef.current = true;
    initializedKeyRef.current = mediaKey;
    setMediaReady(true);
  };

  const seekTo = (nextMs: number) => {
    const media = mediaRef.current;
    if (!media || !progressReadyRef.current) return;
    const bounded = Math.min(durationRef.current, Math.max(0, nextMs));
    media.currentTime = bounded / 1000;
    positionRef.current = bounded;
    setCurrentMs(bounded);
    persist();
  };

  const togglePlay = () => {
    const media = mediaRef.current;
    if (!media) return;
    if (!media.paused) { media.pause(); return; }
    setPlayError(null);
    void media.play().catch((failure: unknown) => {
      if (mediaRef.current === media && !(failure instanceof DOMException && failure.name === "AbortError")) setPlayError(errorMessage(failure));
    });
  };

  const toggleFullscreen = () => {
    if (!onToggleFullscreen) return;
    try {
      void Promise.resolve(onToggleFullscreen()).catch(() => setPlayError("无法切换网页全屏。"));
    } catch {
      setPlayError("无法切换网页全屏。");
    }
  };

  const onKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target instanceof HTMLElement && event.target.closest("button, input, select")) return;
    const key = event.key.toLowerCase();
    if (key === " " || key === "k") togglePlay();
    else if (key === "arrowleft") seekTo(positionRef.current - 10_000);
    else if (key === "arrowright") seekTo(positionRef.current + 10_000);
    else if (key === "m") { const media = mediaRef.current; if (media) { media.muted = !media.muted; setMuted(media.muted); } }
    else if (key === "f") toggleFullscreen();
    else return;
    event.preventDefault();
  };

  if (!playbackUrl) {
    return <div className={styles.videoPlayerUnavailable} role="status">
      {video.playbackStatus === "MISSING" ? "原视频文件已丢失，暂时无法播放。"
        : video.playbackStatus === "PREPARING" ? "播放媒体正在准备，请稍后再试。"
          : video.playbackStatus === "FAILED" ? "当前视频格式无法在浏览器播放。" : "暂无可播放媒体。"}
    </div>;
  }

  return (
    <div className={styles.videoPlayer} tabIndex={0} onKeyDown={onKeyboard} aria-label="视频播放器">
      <div className={styles.videoPlayerStage}>
        <video
          key={mediaKey}
          ref={mediaRef}
          src={playbackUrl}
          poster={mediaUrl(video.thumbnail?.url ?? null) ?? undefined}
          playsInline
          preload="metadata"
          onLoadedMetadata={onLoadedMetadata}
          onTimeUpdate={syncPosition}
          onSeeked={syncPosition}
          onPlay={() => setPlaying(true)}
          onPause={() => { setPlaying(false); persist(); }}
          onEnded={() => { setPlaying(false); syncPosition(); persist(); }}
          onVolumeChange={(event) => { setVolume(event.currentTarget.volume); setMuted(event.currentTarget.muted); }}
          onRateChange={(event) => setPlaybackRate(event.currentTarget.playbackRate)}
          onError={() => { setPlaying(false); setPlayError("视频媒体无法读取，请稍后重试。"); }}
          onClick={togglePlay}
          aria-label={`${episodeLabel(video.episodeOrder)} 视频`}
        />
        {!playing ? <button className={styles.videoPlayerCenterPlay} type="button" onClick={togglePlay} aria-label="播放视频"><Play size={28} fill="currentColor" /></button> : null}
      </div>
      <div className={styles.videoPlayerControls}>
        <input
          className={styles.videoPlayerTimeline}
          type="range"
          min={0}
          max={durationMs}
          step={1000}
          value={Math.min(currentMs, durationMs)}
          disabled={!mediaReady || durationMs <= 0}
          onChange={(event) => seekTo(Number(event.currentTarget.value))}
          aria-label="播放进度"
          aria-valuetext={`${formatTimecode(currentMs)} / ${formatTimecode(durationMs)}`}
        />
        <div className={styles.videoPlayerControlRow}>
          <button type="button" onClick={togglePlay} aria-label={playing ? "暂停" : "播放"}>{playing ? <Pause size={19} fill="currentColor" /> : <Play size={19} fill="currentColor" />}</button>
          <button className={styles.videoPlayerSkip} type="button" disabled={!mediaReady} onClick={() => seekTo(positionRef.current - 10_000)} aria-label="后退 10 秒"><RotateCcw size={18} /><small>10s</small></button>
          <button className={styles.videoPlayerSkip} type="button" disabled={!mediaReady} onClick={() => seekTo(positionRef.current + 10_000)} aria-label="前进 10 秒"><RotateCw size={18} /><small>10s</small></button>
          <time>{formatTimecode(currentMs)} / {formatTimecode(durationMs)}</time>
          <div className={styles.videoPlayerControlSpacer} />
          <button type="button" onClick={() => { const media = mediaRef.current; if (media) { media.muted = !media.muted; if (!media.muted && media.volume === 0) media.volume = 0.5; setMuted(media.muted); setVolume(media.volume); } }} aria-label={muted || volume === 0 ? "取消静音" : "静音"}>
            {muted || volume === 0 ? <VolumeX size={18} /> : <Volume2 size={18} />}
          </button>
          <input className={styles.videoPlayerVolume} type="range" min={0} max={1} step={0.05} value={muted ? 0 : volume} onChange={(event) => { const media = mediaRef.current; if (media) { media.volume = Number(event.currentTarget.value); media.muted = media.volume === 0; setVolume(media.volume); setMuted(media.muted); } }} aria-label="音量" />
          <select value={playbackRate} onChange={(event) => { const media = mediaRef.current; if (media) { media.playbackRate = Number(event.currentTarget.value); setPlaybackRate(media.playbackRate); } }} aria-label="播放倍速">
            {[0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) => <option key={rate} value={rate}>{rate}×</option>)}
          </select>
          <button type="button" onClick={toggleFullscreen} aria-label={fullscreen ? "退出网页全屏" : "网页全屏"} disabled={!onToggleFullscreen}>{fullscreen ? <Minimize size={18} /> : <Maximize size={18} />}</button>
        </div>
      </div>
      {playError ? <p className={styles.videoPlayerError} role="alert">{playError}</p> : null}
    </div>
  );
}
