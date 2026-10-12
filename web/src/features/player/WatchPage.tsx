import { ChevronLeft, ChevronRight, Clapperboard, ListVideo, MessageCircle, PanelRightClose, Play, X } from "lucide-react";
import { type CSSProperties, type Dispatch, type KeyboardEvent, type PointerEvent, type ReactNode, type SetStateAction, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useOutletContext, useParams } from "react-router";

import { errorMessage, getSeries, mediaUrl, shouldRetryApiRequest } from "../../api/client";
import type { WatchPanelLayout } from "../../app/AppShell";
import { episodeLabel, formatTimecode } from "../../shared/format";
import { SearchPage, type SearchPageHandle } from "../search/SearchPage";
import { VideoPlayer, type VideoPlayerHandle } from "./VideoPlayer";
import { PlaybackFrame } from "./PlaybackFrame";
import styles from "./watch.module.css";

function watchPath(seriesId: string, videoId: string): string {
  return `/watch/${encodeURIComponent(seriesId)}/${encodeURIComponent(videoId)}`;
}

const PANEL_WIDTH_KEY = "anivag-watch-panel-width";
const MIN_PANEL_WIDTH = 320;

type PlaybackExtraContext = {
  playback: {
    seriesId: string;
    videoId: string;
    episodeOrder: number;
    /** Integer milliseconds; matches the retained API field name. */
    positionSeconds: number;
  };
};

function savedPanelWidth(): number {
  try {
    const value = Number(window.localStorage.getItem(PANEL_WIDTH_KEY));
    return Number.isFinite(value) && value >= MIN_PANEL_WIDTH && value <= 850 ? value : 440;
  } catch {
    return 440;
  }
}

function WatchPanelPortal({ children }: { children: ReactNode }) {
  const host = document.getElementById("watch-side-panel-host");
  return host ? createPortal(children, host) : children;
}

export function WatchPage() {
  const { seriesId, videoId } = useParams();
  const { search } = useLocation();
  const navigate = useNavigate();
  const setWatchPanelLayout = useOutletContext<Dispatch<SetStateAction<WatchPanelLayout>> | undefined>();
  const pageRef = useRef<HTMLElement>(null);
  const layoutRef = useRef<HTMLDivElement>(null);
  const playerRef = useRef<VideoPlayerHandle>(null);
  const chatRef = useRef<SearchPageHandle>(null);
  const panelToggleRef = useRef<HTMLButtonElement>(null);
  const playbackContextRef = useRef<PlaybackExtraContext | null>(null);
  const previousVideoId = useRef(videoId);
  const draggingRef = useRef(false);
  const pendingChatFocusRef = useRef(false);
  const resumeAfterChatRef = useRef(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelTab, setPanelTab] = useState<"chat" | "episodes">("chat");
  const [panelWidth, setPanelWidth] = useState(savedPanelWidth);
  const [resizing, setResizing] = useState(false);
  const [viewportWidth, setViewportWidth] = useState(window.innerWidth);
  const maxPanelWidth = Math.min(850, Math.max(MIN_PANEL_WIDTH, viewportWidth - 420));
  const visiblePanelWidth = Math.min(panelWidth, maxPanelWidth);
  const [pageFullscreen, setPageFullscreen] = useState(false);
  const seriesQuery = useQuery({
    queryKey: ["series-detail", seriesId],
    queryFn: () => getSeries(seriesId!),
    enabled: Boolean(seriesId),
    retry: shouldRetryApiRequest,
  });
  const series = seriesQuery.data;
  const episodes = [...(series?.videos ?? [])].sort((left, right) =>
    left.episodeOrder - right.episodeOrder || left.uploadSequence - right.uploadSequence,
  );
  const selectedIndex = episodes.findIndex((item) => item.videoId === videoId);
  const selected = selectedIndex >= 0 ? episodes[selectedIndex]! : null;
  const followingEpisodes = selectedIndex >= 0 ? episodes.slice(selectedIndex + 1) : episodes;
  const suggestedEpisodes = followingEpisodes.length ? followingEpisodes : episodes;
  const searchParams = new URLSearchParams(search);
  const rawAtSeconds = searchParams.get("atSeconds");
  const parsedAtSeconds = rawAtSeconds !== null && /^\d+$/.test(rawAtSeconds) ? Number(rawAtSeconds) : undefined;
  const explicitAtSeconds = parsedAtSeconds !== undefined && Number.isSafeInteger(parsedAtSeconds) ? parsedAtSeconds : undefined;
  const rawAtMs = searchParams.get("atMs");
  const parsedAtMs = rawAtMs !== null && /^\d+$/.test(rawAtMs) ? Number(rawAtMs) : undefined;
  const legacyAtSeconds = parsedAtMs !== undefined && Number.isSafeInteger(parsedAtMs) ? parsedAtMs : undefined;
  const atSeconds = explicitAtSeconds ?? legacyAtSeconds;

  const updatePlaybackContext = useCallback((positionSeconds: number) => {
    if (!selected) return;
    playbackContextRef.current = {
      playback: {
        seriesId: selected.seriesId,
        videoId: selected.videoId,
        episodeOrder: selected.episodeOrder,
        positionSeconds,
      },
    };
  }, [selected?.episodeOrder, selected?.seriesId, selected?.videoId]);

  const captureExtraContext = useCallback((): Record<string, unknown> | null => {
    if (!selected) return null;
    const paused = playerRef.current?.pauseAndGetSnapshot();
    if (paused) {
      if (paused.wasPlaying) resumeAfterChatRef.current = true;
      updatePlaybackContext(paused.positionSeconds);
    }
    const context = playbackContextRef.current;
    if (context?.playback.seriesId !== selected.seriesId
      || context.playback.videoId !== selected.videoId
      || context.playback.episodeOrder !== selected.episodeOrder) return null;
    return context;
  }, [selected, updatePlaybackContext]);

  const focusChat = () => {
    captureExtraContext();
    if (panelOpen && panelTab === "chat") {
      pendingChatFocusRef.current = false;
      chatRef.current?.focusComposer();
      return;
    }
    pendingChatFocusRef.current = true;
    setPanelOpen(true);
    setPanelTab("chat");
  };

  const focusPlayer = () => {
    pendingChatFocusRef.current = false;
    playerRef.current?.focus();
    if (!resumeAfterChatRef.current) return;
    resumeAfterChatRef.current = false;
    playerRef.current?.resume();
  };

  const closePanel = () => {
    pendingChatFocusRef.current = false;
    draggingRef.current = false;
    setResizing(false);
    setPanelOpen(false);
    panelToggleRef.current?.focus();
  };

  const switchPaneOnTab = (event: KeyboardEvent<HTMLElement>, destination: "chat" | "player") => {
    if (event.key !== "Tab") return;
    event.preventDefault();
    if (destination === "chat") focusChat();
    else focusPlayer();
  };

  useEffect(() => {
    playbackContextRef.current = null;
  }, [atSeconds, selected?.episodeOrder, selected?.seriesId, selected?.videoId]);

  useEffect(() => {
    if (selected) layoutRef.current?.scrollIntoView?.({ block: "start", behavior: "instant" });
  }, [atSeconds, selected?.seriesId, selected?.videoId]);

  useEffect(() => {
    if (rawAtMs === null) return;
    const canonical = new URLSearchParams(search);
    canonical.delete("atMs");
    if (explicitAtSeconds === undefined && legacyAtSeconds !== undefined) canonical.set("atSeconds", String(legacyAtSeconds));
    const canonicalSearch = canonical.toString();
    navigate({ search: canonicalSearch ? `?${canonicalSearch}` : "" }, { replace: true });
  }, [explicitAtSeconds, legacyAtSeconds, navigate, rawAtMs, search]);

  useEffect(() => {
    if (previousVideoId.current === videoId) return;
    previousVideoId.current = videoId;
    resumeAfterChatRef.current = false;
    void seriesQuery.refetch();
  }, [videoId, seriesQuery.refetch]);

  useEffect(() => {
    if (!pendingChatFocusRef.current || !panelOpen || panelTab !== "chat") return;
    pendingChatFocusRef.current = false;
    chatRef.current?.focusComposer();
  }, [panelOpen, panelTab]);

  useEffect(() => {
    try { window.localStorage.setItem(PANEL_WIDTH_KEY, String(panelWidth)); } catch { /* Resizing still works without storage. */ }
  }, [panelWidth]);

  useEffect(() => {
    const sync = () => setViewportWidth(window.innerWidth);
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  useLayoutEffect(() => {
    setWatchPanelLayout?.({ open: panelOpen, width: visiblePanelWidth, resizing });
  }, [panelOpen, visiblePanelWidth, resizing, setWatchPanelLayout]);

  useLayoutEffect(() => () => {
    setWatchPanelLayout?.((layout) => ({ ...layout, open: false, resizing: false }));
  }, [setWatchPanelLayout]);

  useEffect(() => {
    const sync = () => setPageFullscreen(Boolean(pageRef.current && document.fullscreenElement?.contains(pageRef.current)));
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  const togglePageFullscreen = async () => {
    const target = pageRef.current?.closest<HTMLElement>("[data-app-shell]") ?? pageRef.current;
    if (document.fullscreenElement === target) {
      await document.exitFullscreen();
    } else if (target?.requestFullscreen) {
      await target.requestFullscreen();
    } else {
      throw new Error("当前浏览器不支持网页全屏");
    }
  };

  const resizePanel = (clientX: number) => {
    const bounds = (pageRef.current?.closest("[data-app-shell]") ?? pageRef.current)?.getBoundingClientRect();
    if (!bounds) return;
    setPanelWidth(Math.max(MIN_PANEL_WIDTH, Math.min(maxPanelWidth, bounds.right - clientX)));
  };

  const endResize = (event: PointerEvent<HTMLDivElement>) => {
    draggingRef.current = false;
    setResizing(false);
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  if (!seriesId) return <main className={`page ${styles.page}`}><p role="alert">视频地址无效。</p></main>;
  if (seriesQuery.isPending) return <main className={`page ${styles.page}`}><p role="status">正在读取视频系列…</p></main>;
  if (seriesQuery.isError) return <main className={`page ${styles.page}`}><p role="alert">读取视频系列失败：{errorMessage(seriesQuery.error)}</p></main>;
  if (!series) return <main className={`page ${styles.page}`}><p role="alert">视频系列不存在。</p></main>;

  const headerActions = !pageFullscreen ? document.getElementById("watch-header-actions") : null;
  const panelToggle = <button ref={panelToggleRef} className={styles.panelToggle} type="button" onClick={() => { if (panelOpen) closePanel(); else focusChat(); }} aria-expanded={panelOpen} aria-controls="watch-side-panel" aria-label={panelOpen ? "收起观看侧栏" : "打开观看侧栏"} title={panelOpen ? "收起对话" : "与 Seekino 对话"}>
    {panelOpen ? <PanelRightClose size={21} aria-hidden="true" /> : <MessageCircle size={21} aria-hidden="true" />}
  </button>;

  return (
    <main ref={pageRef} className={`page ${styles.page}`} data-panel-open={panelOpen} data-fullscreen={pageFullscreen} style={{ "--watch-panel-width": `${visiblePanelWidth}px` } as CSSProperties}>
      <div ref={layoutRef} className={styles.layout}>
        {headerActions ? createPortal(panelToggle, headerActions) : <div className={styles.floatingTools}>{panelToggle}</div>}
        <section className={styles.viewer} aria-label="视频播放器" onKeyDownCapture={(event) => switchPaneOnTab(event, "chat")}>
          {episodes.length === 0 ? <p className={styles.unavailable} role="status">这个系列还没有视频。</p> : !selected ? (
            <p className={styles.unavailable} role="alert">找不到这集视频，请在下方选集重新选择。</p>
          ) : <VideoPlayer ref={playerRef} key={`${selected.videoId}:${atSeconds ?? "resume"}`} video={selected} atSeconds={atSeconds} fullscreen={pageFullscreen} onToggleFullscreen={togglePageFullscreen} onPlaybackPositionChange={updatePlaybackContext} />}
        </section>
      </div>

      <WatchPanelPortal>
      <aside id="watch-side-panel" className={styles.sidePanel} aria-label="观看侧栏" data-open={panelOpen} aria-hidden={!panelOpen} inert={!panelOpen} onKeyDown={(event) => { if (event.key === "Escape" && !event.defaultPrevented) { event.stopPropagation(); closePanel(); } }}>
        {panelOpen ? <div
          className={styles.resizeHandle}
          role="separator"
          tabIndex={0}
          aria-label="调整右侧栏宽度"
          aria-orientation="vertical"
          aria-valuemin={MIN_PANEL_WIDTH}
          aria-valuemax={maxPanelWidth}
          aria-valuenow={visiblePanelWidth}
          onPointerDown={(event) => {
            event.preventDefault();
            draggingRef.current = true;
            setResizing(true);
            event.currentTarget.setPointerCapture?.(event.pointerId);
            resizePanel(event.clientX);
          }}
          onPointerMove={(event) => { if (draggingRef.current) resizePanel(event.clientX); }}
          onPointerUp={endResize}
          onPointerCancel={endResize}
          onKeyDown={(event) => {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            setPanelWidth(Math.max(MIN_PANEL_WIDTH, Math.min(maxPanelWidth, visiblePanelWidth + (event.key === "ArrowLeft" ? 24 : -24))));
          }}
        /> : null}

        <div className={styles.panelHeader}><strong>边看边聊</strong><button type="button" onClick={closePanel} aria-label="关闭对话面板"><X size={20} aria-hidden="true" /></button></div>
        <div className={styles.sideTabs} role="tablist" aria-label="观看侧栏内容">
          <button type="button" role="tab" id="watch-chat-tab" aria-controls="watch-chat-panel" aria-selected={panelTab === "chat"} onClick={focusChat}><MessageCircle size={15} aria-hidden="true" />Chat</button>
          <button type="button" role="tab" id="watch-episodes-tab" aria-controls="watch-episodes-panel" aria-selected={panelTab === "episodes"} onClick={() => setPanelTab("episodes")}><ListVideo size={15} aria-hidden="true" />选集</button>
        </div>
        <div
          id="watch-chat-panel"
          className={styles.chatPanel}
          role="tabpanel"
          aria-labelledby="watch-chat-tab"
          hidden={panelTab !== "chat"}
          onPointerDownCapture={(event) => { if (event.button === 0) captureExtraContext(); }}
          onKeyDownCapture={(event) => switchPaneOnTab(event, "player")}
        >
          <SearchPage ref={chatRef} embedded initialSeriesId={seriesId} captureExtraContext={captureExtraContext} onOpenVideo={({ seriesId: targetSeriesId, videoId: targetVideoId, startSeconds }) => {
            navigate(`${watchPath(targetSeriesId, targetVideoId)}?atSeconds=${startSeconds}`);
          }} />
        </div>
        <div id="watch-episodes-panel" className={styles.episodePanel} role="tabpanel" aria-labelledby="watch-episodes-tab" hidden={panelTab !== "episodes"}>
          <h2>选集 <span><small>{episodes.length} 集</small><Link to="/library">调整集数</Link></span></h2>
          <ol className={styles.episodeList}>
            {episodes.map((episode) => (
              <li key={episode.videoId}>
                <Link to={watchPath(seriesId, episode.videoId)} aria-current={episode.videoId === selected?.videoId ? "page" : undefined}>
                  <span>{episodeLabel(episode.episodeOrder)}</span>
                  <strong>{episode.originalFilename}</strong>
                  <small>{formatTimecode(episode.durationSeconds)} · {episode.playbackStatus === "READY" ? "可播放" : "播放未就绪"}</small>
                </Link>
              </li>
            ))}
          </ol>
        </div>
      </aside>
      </WatchPanelPortal>
      <div className={styles.watchDetails}>
        <section className={styles.watchSynopsis} aria-label="视频简介">
          <Link className={styles.seriesName} to={`/series/${encodeURIComponent(seriesId)}`}>{series.name}<ChevronRight size={18} aria-hidden="true" /></Link>
          <h1>{selected ? `${episodeLabel(selected.episodeOrder)} · ${selected.originalFilename}` : series.name}</h1>
          <div className={styles.videoMeta}><span>{selected ? formatTimecode(selected.durationSeconds) : null}</span><span>共 {episodes.length} 集</span></div>
          {series.description ? <p>{series.description}</p> : null}
          {selected ? <nav className={styles.neighbors} aria-label="切换集数">
            {selectedIndex > 0 ? <Link to={watchPath(seriesId, episodes[selectedIndex - 1]!.videoId)}><ChevronLeft size={16} />上一集</Link> : null}
            {selectedIndex < episodes.length - 1 ? <Link to={watchPath(seriesId, episodes[selectedIndex + 1]!.videoId)}>下一集<ChevronRight size={16} /></Link> : null}
          </nav> : null}
        </section>
        <section className={styles.upNext} aria-label="后续集数">
          <h2>{followingEpisodes.length ? "接下来" : "本系列选集"}</h2>
          <ol className={styles.nextEpisodes}>
            {suggestedEpisodes.map((episode) => <li key={episode.videoId}>
              <Link to={watchPath(seriesId, episode.videoId)} aria-current={episode.videoId === selected?.videoId ? "page" : undefined}>
                <span className={styles.nextPreview} aria-hidden="true">
                  <Clapperboard size={24} strokeWidth={1} />
                  {episode.thumbnail ? <img src={mediaUrl(episode.thumbnail.url) ?? undefined} alt="" loading="lazy" onError={(event) => { event.currentTarget.hidden = true; }} />
                    : episode.playbackStatus === "READY" && episode.playbackUrl ? <PlaybackFrame className={styles.seriesFrame} src={mediaUrl(episode.playbackUrl)} positionSeconds={episode.durationSeconds * 0.1} /> : null}
                  <span className={styles.nextPlay}><Play size={17} fill="currentColor" /></span>
                </span>
                <span className={styles.nextTitle}><small>{episodeLabel(episode.episodeOrder)}{episode.videoId === selected?.videoId ? " · 当前集" : ""}</small><strong>{episode.originalFilename}</strong><span>{formatTimecode(episode.durationSeconds)}{episode.playbackStatus !== "READY" ? " · 播放未就绪" : ""}</span></span>
              </Link>
            </li>)}
          </ol>
        </section>
      </div>
    </main>
  );
}
