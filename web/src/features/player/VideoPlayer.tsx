import { Maximize, Minimize, Pause, Play, RotateCcw, RotateCw, Search, Volume2, VolumeX } from "lucide-react";
import { forwardRef, type CSSProperties, type KeyboardEvent, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from "react";

import { errorMessage, mediaUrl } from "../../api/client";
import type { VideoSummary } from "../../api/contracts";
import { episodeLabel, formatTimecode } from "../../shared/format";
import { playbackProgressStorageKey, readVideoProgress, saveVideoProgress } from "./playbackProgress";
import { useI18n } from "../../app/i18n";
import { VideoTextFind } from "./VideoTextFind";
import styles from "./watch.module.css";

interface VideoPlayerProps {
  video: VideoSummary;
  /** Integer milliseconds; the public field name is retained. */
  atSeconds?: number;
  fullscreen?: boolean;
  onToggleFullscreen?: () => void | Promise<void>;
  onPlaybackPositionChange?: (positionSeconds: number) => void;
}

export interface VideoPlayerHandle {
  /** positionSeconds is integer milliseconds, including subsecond playback position. */
  getSnapshot(): { positionSeconds: number; wasPlaying: boolean } | null;
  pauseAndGetSnapshot(): { positionSeconds: number; wasPlaying: boolean } | null;
  focus(): void;
  resume(): void;
}

const PROGRESS_WRITE_INTERVAL_MS = 5_000;
const CONTROLS_HIDE_DELAY_MS = 5_000;

export const VideoPlayer = forwardRef<VideoPlayerHandle, VideoPlayerProps>(function VideoPlayer(
  { video, atSeconds, fullscreen = false, onToggleFullscreen, onPlaybackPositionChange },
  ref,
) {
  const { t, locale } = useI18n();
  const mediaRef = useRef<HTMLVideoElement>(null);
  const playerRef = useRef<HTMLDivElement>(null);
  const findInputRef = useRef<HTMLInputElement>(null);
  const [findOpen, setFindOpen] = useState(false);
  const positionRef = useRef(0);
  const durationRef = useRef(video.durationSeconds);
  const progressReadyRef = useRef(false);
  const lastWriteAtRef = useRef(0);
  const initializedKeyRef = useRef<string | null>(null);
  const [progressStorageKey] = useState(playbackProgressStorageKey);
  const [currentSeconds, setCurrentSeconds] = useState(0);
  const [durationSeconds, setDurationSeconds] = useState(video.durationSeconds);
  const [mediaReady, setMediaReady] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [playError, setPlayError] = useState<string | null>(null);
  const [controlsVisible, setControlsVisible] = useState(true);
  const [pointerInside, setPointerInside] = useState(false);
  const [pointerHeld, setPointerHeld] = useState(false);
  const [keyboardFocus, setKeyboardFocus] = useState(false);
  const playbackUrl = video.playbackStatus === "READY" ? mediaUrl(video.playbackUrl) : null;
  const mediaKey = `${video.videoId}:${atSeconds ?? "history"}:${playbackUrl ?? "none"}`;

  const openFind = useCallback((selectQuery = false) => {
    setFindOpen(true);
    findInputRef.current?.focus();
    if (selectQuery) findInputRef.current?.select();
  }, []);

  useEffect(() => {
    if (!playbackUrl) return;
    const onFindShortcut = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
      if (event.metaKey === event.ctrlKey) return;
      if (event.altKey || event.shiftKey || event.key.toLowerCase() !== "f") return;
      event.preventDefault();
      event.stopPropagation();
      openFind(true);
    };
    window.addEventListener("keydown", onFindShortcut, true);
    return () => window.removeEventListener("keydown", onFindShortcut, true);
  }, [openFind, playbackUrl]);

  useEffect(() => {
    setControlsVisible(true);
    if (pointerInside || pointerHeld || keyboardFocus) return;
    const timeout = window.setTimeout(() => setControlsVisible(false), CONTROLS_HIDE_DELAY_MS);
    return () => window.clearTimeout(timeout);
  }, [mediaKey, pointerInside, pointerHeld, keyboardFocus]);

  useEffect(() => {
    if (!pointerHeld) return;
    // A seek or volume drag can end outside the player.
    const release = () => setPointerHeld(false);
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    window.addEventListener("blur", release);
    return () => {
      window.removeEventListener("pointerup", release);
      window.removeEventListener("pointercancel", release);
      window.removeEventListener("blur", release);
    };
  }, [pointerHeld]);

  useLayoutEffect(() => {
    // Metadata may already be available for a cached source. A duration refresh
    // does not replace the media element or invalidate an initialized player.
    if (initializedKeyRef.current !== mediaKey) {
      positionRef.current = 0;
      durationRef.current = video.durationSeconds;
      progressReadyRef.current = false;
      lastWriteAtRef.current = 0;
      initializedKeyRef.current = null;
      setCurrentSeconds(0);
      setDurationSeconds(video.durationSeconds);
      setMediaReady(false);
      setPlaying(false);
      setVolume(1);
      setMuted(false);
      setPlaybackRate(1);
      setPlayError(null);
      if (mediaRef.current && mediaRef.current.readyState >= HTMLMediaElement.HAVE_METADATA) onLoadedMetadata();
    }
    return () => {
      if (progressReadyRef.current) {
        saveVideoProgress({ seriesId: video.seriesId, videoId: video.videoId, positionSeconds: positionRef.current, durationSeconds: durationRef.current }, progressStorageKey);
      }
    };
  }, [video.seriesId, video.videoId, mediaKey, video.durationSeconds, progressStorageKey]);

  useEffect(() => {
    const flush = () => {
      if (progressReadyRef.current) {
        saveVideoProgress({ seriesId: video.seriesId, videoId: video.videoId, positionSeconds: positionRef.current, durationSeconds: durationRef.current }, progressStorageKey);
      }
    };
    const flushWhenHidden = () => { if (document.visibilityState === "hidden") flush(); };
    window.addEventListener("pagehide", flush);
    document.addEventListener("visibilitychange", flushWhenHidden);
    return () => {
      window.removeEventListener("pagehide", flush);
      document.removeEventListener("visibilitychange", flushWhenHidden);
    };
  }, [video.seriesId, video.videoId, progressStorageKey]);

  const persist = () => {
    if (!progressReadyRef.current) return;
    saveVideoProgress({ seriesId: video.seriesId, videoId: video.videoId, positionSeconds: positionRef.current, durationSeconds: durationRef.current }, progressStorageKey);
    lastWriteAtRef.current = Date.now();
  };

  const updatePosition = (positionSeconds: number) => {
    positionRef.current = positionSeconds;
    setCurrentSeconds(positionSeconds);
    onPlaybackPositionChange?.(positionSeconds);
  };

  const syncPosition = () => {
    const media = mediaRef.current;
    if (!media) return;
    const next = Math.max(0, Math.round(media.currentTime * 1000));
    updatePosition(next);
    if (Date.now() - lastWriteAtRef.current >= PROGRESS_WRITE_INTERVAL_MS) persist();
  };

  const readSnapshot = () => {
    const media = mediaRef.current;
    if (!media || !progressReadyRef.current) return null;
    return {
      positionSeconds: Math.max(0, Math.round(media.currentTime * 1000)),
      wasPlaying: !media.paused && !media.ended,
    };
  };

  const pauseAndReadPosition = () => {
    const snapshot = readSnapshot();
    if (!snapshot) return null;
    mediaRef.current?.pause();
    updatePosition(snapshot.positionSeconds);
    persist();
    return snapshot;
  };

  const resume = () => {
    const media = mediaRef.current;
    if (!media || media.ended || !media.paused) return;
    setPlayError(null);
    void media.play().catch((failure: unknown) => {
      if (mediaRef.current === media && !(failure instanceof DOMException && failure.name === "AbortError")) setPlayError(errorMessage(failure, locale));
    });
  };

  useImperativeHandle(ref, () => ({
    getSnapshot: readSnapshot,
    pauseAndGetSnapshot: pauseAndReadPosition,
    focus: () => playerRef.current?.focus(),
    resume,
  }));

  const onLoadedMetadata = () => {
    const media = mediaRef.current;
    if (!media || initializedKeyRef.current === mediaKey) return;
    const knownDuration = Number.isFinite(media.duration) && media.duration > 0 ? Math.round(media.duration * 1000) : video.durationSeconds;
    durationRef.current = knownDuration;
    setDurationSeconds(knownDuration);
    const saved = readVideoProgress(video.videoId, progressStorageKey);
    const resume = saved?.seriesId === video.seriesId ? saved.positionSeconds : 0;
    const nearEnd = knownDuration > 0 && resume >= knownDuration - Math.min(15_000, knownDuration * 0.05);
    const requested = atSeconds !== undefined && Number.isSafeInteger(atSeconds) && atSeconds >= 0 ? atSeconds : nearEnd ? 0 : resume;
    const startSeconds = Math.min(knownDuration, Math.max(0, requested));
    media.currentTime = startSeconds / 1000;
    updatePosition(startSeconds);
    progressReadyRef.current = true;
    initializedKeyRef.current = mediaKey;
    setMediaReady(true);
  };

  const seekTo = (nextSeconds: number) => {
    const media = mediaRef.current;
    if (!media || !progressReadyRef.current) return;
    const bounded = Math.min(durationRef.current, Math.max(0, nextSeconds));
    media.currentTime = bounded / 1000;
    updatePosition(bounded);
    persist();
  };

  const togglePlay = () => {
    const media = mediaRef.current;
    if (!media) return;
    if (!media.paused) { media.pause(); return; }
    resume();
  };

  const toggleFullscreen = async (target: "player" | "page") => {
    try {
      if (target === "page") {
        await onToggleFullscreen?.();
      } else if (document.fullscreenElement === playerRef.current) {
        await document.exitFullscreen();
      } else {
        await playerRef.current?.requestFullscreen();
      }
    } catch {
      setPlayError(t("无法切换网页全屏。"));
    }
  };

  const onKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.defaultPrevented || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target instanceof HTMLElement && (target.isContentEditable || target.closest('input, textarea, select, [role="search"]'))) return;
    const key = event.key.toLowerCase();
    if (key === "/" && !event.shiftKey) {
      event.preventDefault();
      openFind(true);
      return;
    }
    if (key === "f") {
      event.preventDefault();
      if (!event.repeat) void toggleFullscreen(event.shiftKey ? "page" : "player");
      return;
    }
    if (target instanceof HTMLElement && target.closest("button")) return;
    if (key === " " || key === "k") togglePlay();
    else if (key === "arrowleft") seekTo(positionRef.current - 10_000);
    else if (key === "arrowright") seekTo(positionRef.current + 10_000);
    else if (key === "m") { const media = mediaRef.current; if (media) { media.muted = !media.muted; setMuted(media.muted); } }
    else return;
    event.preventDefault();
  };

  if (!playbackUrl) {
    return <div className={styles.videoPlayerUnavailable} role="status">
      {video.playbackStatus === "MISSING" ? t("原视频文件已丢失，暂时无法播放。")
        : video.playbackStatus === "PREPARING" ? t("播放媒体正在准备，请稍后再试。")
          : video.playbackStatus === "FAILED" ? t("当前视频格式无法在浏览器播放。") : t("暂无可播放媒体。")}
    </div>;
  }

  const timelinePosition = Math.min(currentSeconds, durationSeconds);
  const timelinePercent = durationSeconds > 0 ? timelinePosition / durationSeconds * 100 : 0;

  return (
    <div ref={playerRef} className={styles.videoPlayer} tabIndex={0} onKeyDown={onKeyboard} aria-label={t("视频播放器")} aria-keyshortcuts="F"
      data-controls-visible={controlsVisible}
      onPointerEnter={() => setPointerInside(true)}
      onPointerLeave={() => setPointerInside(false)}
      onPointerDownCapture={() => { setKeyboardFocus(false); setPointerHeld(true); }}
      onKeyDownCapture={() => setKeyboardFocus(true)}
      onFocusCapture={(event) => { if (event.target.matches(":focus-visible")) setKeyboardFocus(true); }}
      onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setKeyboardFocus(false); }}>
      <div className={styles.videoPlayerStage} onClickCapture={() => playerRef.current?.focus({ preventScroll: true })}>
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
          onError={() => { setPlaying(false); setPlayError(t("视频媒体无法读取，请稍后重试。")); }}
          onClick={togglePlay}
          aria-label={t("{episode} 视频", { episode: episodeLabel(video.episodeOrder) })}
        />
        {!playing ? <button className={styles.videoPlayerCenterPlay} type="button" onClick={togglePlay} aria-label={t("播放视频")}><Play size={28} fill="currentColor" /></button> : null}
      </div>
      {findOpen && <VideoTextFind key={`${video.tenantId}:${video.videoId}`} videoId={video.videoId}
        tenantId={video.tenantId} inputRef={findInputRef} canSeek={mediaReady} onSeek={seekTo}
        onClose={() => { setFindOpen(false); playerRef.current?.focus(); }} />}
      <div className={styles.videoPlayerControls}>
        <input
          className={styles.videoPlayerTimeline}
          type="range"
          min={0}
          max={durationSeconds}
          step={1}
          value={timelinePosition}
          style={{ "--timeline-progress": `${timelinePercent}%` } as CSSProperties}
          disabled={!mediaReady || durationSeconds <= 0}
          onChange={(event) => seekTo(Number(event.currentTarget.value))}
          aria-label={t("播放进度")}
          aria-valuetext={`${formatTimecode(currentSeconds)} / ${formatTimecode(durationSeconds)}`}
        />
        <div className={styles.videoPlayerControlRow}>
          <button type="button" onClick={togglePlay} aria-label={playing ? t("暂停") : t("播放")}>{playing ? <Pause size={19} fill="currentColor" /> : <Play size={19} fill="currentColor" />}</button>
          <button className={styles.videoPlayerSkip} type="button" disabled={!mediaReady} onClick={() => seekTo(positionRef.current - 10_000)} aria-label={t("后退 10 秒")}><RotateCcw size={18} /><small>10s</small></button>
          <button className={styles.videoPlayerSkip} type="button" disabled={!mediaReady} onClick={() => seekTo(positionRef.current + 10_000)} aria-label={t("前进 10 秒")}><RotateCw size={18} /><small>10s</small></button>
          <time>{formatTimecode(currentSeconds)} / {formatTimecode(durationSeconds)}</time>
          <div className={styles.videoPlayerControlSpacer} />
          <button type="button" aria-label={t("在本视频中查找")} title={`${t("在本视频中查找")} (Ctrl/⌘+F, /)`}
            aria-keyshortcuts="Control+F Meta+F /" aria-expanded={findOpen} onClick={() => openFind()}><Search size={18} /></button>
          <button type="button" onClick={() => { const media = mediaRef.current; if (media) { media.muted = !media.muted; if (!media.muted && media.volume === 0) media.volume = 0.5; setMuted(media.muted); setVolume(media.volume); } }} aria-label={muted || volume === 0 ? t("取消静音") : t("静音")}>
            {muted || volume === 0 ? <VolumeX size={18} /> : <Volume2 size={18} />}
          </button>
          <input className={styles.videoPlayerVolume} type="range" min={0} max={1} step={0.05} value={muted ? 0 : volume} onChange={(event) => { const media = mediaRef.current; if (media) { media.volume = Number(event.currentTarget.value); media.muted = media.volume === 0; setVolume(media.volume); setMuted(media.muted); } }} aria-label={t("音量")} />
          <select value={playbackRate} onChange={(event) => { const media = mediaRef.current; if (media) { media.playbackRate = Number(event.currentTarget.value); setPlaybackRate(media.playbackRate); } }} aria-label={t("播放倍速")}>
            {[0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) => <option key={rate} value={rate}>{rate}×</option>)}
          </select>
          <button type="button" onClick={() => void toggleFullscreen("page")} aria-label={fullscreen ? t("退出网页全屏") : t("网页全屏")}
            title={`${fullscreen ? t("退出网页全屏") : t("网页全屏")} (Shift+F)`} aria-keyshortcuts="Shift+F"
            disabled={!onToggleFullscreen}>{fullscreen ? <Minimize size={18} /> : <Maximize size={18} />}</button>
        </div>
      </div>
      {playError ? <p className={styles.videoPlayerError} role="alert">{playError}</p> : null}
    </div>
  );
});
