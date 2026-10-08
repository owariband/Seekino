import { ChevronLeft, ChevronRight, ListVideo, MessageCircle, PanelRightClose, PanelRightOpen } from "lucide-react";
import { type CSSProperties, type KeyboardEvent, type PointerEvent, useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { errorMessage, getSeries, shouldRetryApiRequest } from "../../api/client";
import { episodeLabel, formatTimecode } from "../../shared/format";
import { SearchPage, type SearchPageHandle } from "../search/SearchPage";
import { VideoPlayer, type VideoPlayerHandle } from "./VideoPlayer";
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

export function WatchPage() {
  const { seriesId, videoId } = useParams();
  const { search } = useLocation();
  const navigate = useNavigate();
  const pageRef = useRef<HTMLElement>(null);
  const layoutRef = useRef<HTMLDivElement>(null);
  const playerRef = useRef<VideoPlayerHandle>(null);
  const chatRef = useRef<SearchPageHandle>(null);
  const playbackContextRef = useRef<PlaybackExtraContext | null>(null);
  const previousVideoId = useRef(videoId);
  const draggingRef = useRef(false);
  const pendingChatFocusRef = useRef(false);
  const resumeAfterChatRef = useRef(false);
  const [panelOpen, setPanelOpen] = useState(() => window.innerWidth > 800);
  const [panelTab, setPanelTab] = useState<"chat" | "episodes">("chat");
  const [panelWidth, setPanelWidth] = useState(savedPanelWidth);
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
    const sync = () => setPageFullscreen(document.fullscreenElement === pageRef.current);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  const togglePageFullscreen = async () => {
    if (document.fullscreenElement === pageRef.current) {
      await document.exitFullscreen();
    } else if (pageRef.current?.requestFullscreen) {
      await pageRef.current.requestFullscreen();
    } else {
      throw new Error("当前浏览器不支持网页全屏");
    }
  };

  const resizePanel = (clientX: number) => {
    const bounds = layoutRef.current?.getBoundingClientRect();
    if (!bounds) return;
    const maxWidth = Math.min(850, Math.max(MIN_PANEL_WIDTH, bounds.width - 420));
    setPanelWidth(Math.max(MIN_PANEL_WIDTH, Math.min(maxWidth, bounds.right - clientX)));
  };

  const endResize = (event: PointerEvent<HTMLDivElement>) => {
    draggingRef.current = false;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  if (!seriesId) return <main className={`page ${styles.page}`}><p role="alert">视频地址无效。</p></main>;
  if (seriesQuery.isPending) return <main className={`page ${styles.page}`}><p role="status">正在读取视频系列…</p></main>;
  if (seriesQuery.isError) return <main className={`page ${styles.page}`}><p role="alert">读取视频系列失败：{errorMessage(seriesQuery.error)}</p></main>;
  if (!series) return <main className={`page ${styles.page}`}><p role="alert">视频系列不存在。</p></main>;

  return (
    <main ref={pageRef} className={`page ${styles.page}`}>
      <header className={styles.header}>
        <div className={styles.headerTitle}>
          <Link to="/watch" className={styles.back}><ChevronLeft size={16} />返回视频</Link>
          <span className={styles.headerDivider} aria-hidden="true" />
          <h1>{series.name}</h1>
          {selected ? <span className={styles.headerEpisode}>{episodeLabel(selected.episodeOrder)}</span> : null}
        </div>
        <button className={styles.panelToggle} type="button" onClick={() => { if (panelOpen) setPanelOpen(false); else focusChat(); }} aria-expanded={panelOpen} aria-controls="watch-side-panel" aria-label={panelOpen ? "收起观看侧栏" : "打开观看侧栏"}>
          {panelOpen ? <PanelRightClose size={18} aria-hidden="true" /> : <PanelRightOpen size={18} aria-hidden="true" />}
          <span>{panelOpen ? "收起侧栏" : "打开 Chat"}</span>
        </button>
      </header>

      <div ref={layoutRef} className={styles.layout} data-panel-open={panelOpen} style={{ "--watch-panel-width": `${panelWidth}px` } as CSSProperties}>
        <section className={styles.viewer} aria-label="视频播放器" onKeyDownCapture={(event) => switchPaneOnTab(event, "chat")}>
          {episodes.length === 0 ? <p className={styles.unavailable} role="status">这个系列还没有视频。</p> : !selected ? (
            <p className={styles.unavailable} role="alert">找不到这集视频，请在右侧选集重新选择。</p>
          ) : <VideoPlayer ref={playerRef} key={`${selected.videoId}:${atSeconds ?? "resume"}`} video={selected} atSeconds={atSeconds} fullscreen={pageFullscreen} onToggleFullscreen={togglePageFullscreen} onPlaybackPositionChange={updatePlaybackContext} />}
          {selected ? <>
            <div className={styles.videoMeta}>
              <div><strong>{episodeLabel(selected.episodeOrder)}</strong><span>{selected.originalFilename}</span></div>
              <span>{formatTimecode(selected.durationSeconds)}</span>
            </div>
            <nav className={styles.neighbors} aria-label="切换集数">
              {selectedIndex > 0 ? <Link to={watchPath(seriesId, episodes[selectedIndex - 1]!.videoId)}><ChevronLeft size={16} />上一集</Link> : <span />}
              {selectedIndex < episodes.length - 1 ? <Link to={watchPath(seriesId, episodes[selectedIndex + 1]!.videoId)}>下一集<ChevronRight size={16} /></Link> : <span />}
            </nav>
          </> : null}
        </section>

        {panelOpen ? <div
          className={styles.resizeHandle}
          role="separator"
          tabIndex={0}
          aria-label="调整右侧栏宽度"
          aria-orientation="vertical"
          aria-valuemin={MIN_PANEL_WIDTH}
          aria-valuemax={850}
          aria-valuenow={panelWidth}
          onPointerDown={(event) => {
            event.preventDefault();
            draggingRef.current = true;
            event.currentTarget.setPointerCapture?.(event.pointerId);
            resizePanel(event.clientX);
          }}
          onPointerMove={(event) => { if (draggingRef.current) resizePanel(event.clientX); }}
          onPointerUp={endResize}
          onPointerCancel={endResize}
          onKeyDown={(event) => {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            const maxWidth = Math.min(850, Math.max(MIN_PANEL_WIDTH, (layoutRef.current?.clientWidth ?? window.innerWidth) - 420));
            setPanelWidth((width) => Math.max(MIN_PANEL_WIDTH, Math.min(maxWidth, width + (event.key === "ArrowLeft" ? 24 : -24))));
          }}
        /> : null}

        <aside id="watch-side-panel" className={styles.sidePanel} aria-label="观看侧栏" hidden={!panelOpen}>
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
      </div>
    </main>
  );
}
