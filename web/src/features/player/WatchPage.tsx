import { ChevronLeft, ChevronRight, Clapperboard, ListVideo, Maximize2, MessageCircle, Minimize2, Play, Settings2, X } from "lucide-react";
import { type CSSProperties, type Dispatch, type KeyboardEvent, type PointerEvent, type ReactNode, type SetStateAction, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { useQuery } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useOutletContext, useParams } from "react-router";

import { errorMessage, getSeries, mediaUrl, shouldRetryApiRequest, TENANT_ID } from "../../api/client";
import type { VideoSummary } from "../../api/contracts";
import type { WatchPanelLayout } from "../../app/AppShell";
import { episodeLabel, videoTitle, formatTimecode, previewFramePosition, seriesTitle } from "../../shared/format";
import { Mascot } from "../../shared/Mascot";
import { Kino } from "../../shared/kino/Kino";
import { useAssistantBusy } from "../../shared/assistantActivity";
import { SearchPage, type SearchPageHandle } from "../search/SearchPage";
import { VideoPlayer, type VideoPlayerHandle } from "./VideoPlayer";
import { PlaybackFrame } from "./PlaybackFrame";
import { useWatchPreferences } from "./watchPreferences";
import { useI18n } from "../../app/i18n";
import { seriesPath, watchPath } from "../library/seriesLinks";
import { useAuth } from "../auth/AuthProvider";
import styles from "./watch.module.css";

const PANEL_WIDTH_KEY = "seekino-watch-panel-width";
const PANEL_MODE_KEY = "seekino-watch-panel-mode";
const KINO_DOCK_KEY = "seekino-kino-entry-dock";
const KINO_SIZE = 64;
const HEADER_HEIGHT = 64;

type KinoDock = { side: "left" | "right"; bottom: number };

function savedKinoDock(): KinoDock {
  try {
    const value = JSON.parse(window.localStorage.getItem(KINO_DOCK_KEY) ?? "null") as Partial<KinoDock> | null;
    if ((value?.side === "left" || value?.side === "right") && Number.isFinite(value.bottom)) return { side: value.side, bottom: value.bottom! };
  } catch { /* Fall back to the default corner. */ }
  return { side: "right", bottom: 84 };
}

/** Keeps Kino between the top bar and the bottom edge of the viewport. */
function clampKinoBottom(bottom: number, viewportHeight: number, topInset = HEADER_HEIGHT): number {
  return Math.round(Math.max(16, Math.min(bottom, viewportHeight - topInset - KINO_SIZE - 8)));
}

/** Playback position for the "can see" chip, published once per second so Chat itself does not re-render. */
function createPositionStore() {
  let position = 0;
  const listeners = new Set<() => void>();
  return {
    get: () => position,
    set(next: number) {
      const changed = Math.floor(next / 1000) !== Math.floor(position / 1000);
      position = next;
      if (changed) listeners.forEach((listener) => listener());
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
  };
}

function PlaybackContextChip({ store, episodeOrder }: { store: ReturnType<typeof createPositionStore>; episodeOrder: number }) {
  const { t } = useI18n();
  const second = useSyncExternalStore(store.subscribe, () => Math.floor(store.get() / 1000));
  return <p className={styles.contextChip}>
    <span>{t("Kino 能看到 {episode} · {time}", { episode: episodeLabel(episodeOrder), time: formatTimecode(second * 1000) })}</span>
  </p>;
}
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

function EpisodeLink({ video, to, current, children }: { video: VideoSummary; to: string; current?: boolean; children: ReactNode }) {
  if (video.playbackStatus !== "READY" || !video.playbackUrl) return <span aria-disabled="true">{children}</span>;
  return <Link to={to} aria-current={current ? "page" : undefined}>{children}</Link>;
}

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
  const { t, locale } = useI18n();
  const { canManage } = useAuth();
  const { seriesId, videoId } = useParams();
  const { search } = useLocation();
  const searchParams = new URLSearchParams(search);
  const tenantId = searchParams.get("tenantId") || TENANT_ID;
  const navigate = useNavigate();
  const setWatchPanelLayout = useOutletContext<Dispatch<SetStateAction<WatchPanelLayout>> | undefined>();
  const pageRef = useRef<HTMLElement>(null);
  const layoutRef = useRef<HTMLDivElement>(null);
  const playerRef = useRef<VideoPlayerHandle>(null);
  const chatRef = useRef<SearchPageHandle>(null);
  const panelToggleRef = useRef<HTMLButtonElement>(null);
  const settingsRef = useRef<HTMLDetailsElement>(null);
  const playbackContextRef = useRef<PlaybackExtraContext | null>(null);
  const previousVideoId = useRef(videoId);
  const draggingRef = useRef(false);
  const pendingChatFocusRef = useRef(false);
  const resumeAfterChatRef = useRef(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelTab, setPanelTab] = useState<"chat" | "episodes">("chat");
  const [panelMode, setPanelMode] = useState<"floating" | "docked">(() => {
    try { return window.localStorage.getItem(PANEL_MODE_KEY) === "docked" ? "docked" : "floating"; }
    catch { return "floating"; }
  });
  const [entryHovered, setEntryHovered] = useState(false);
  const [kinoDock, setKinoDock] = useState(savedKinoDock);
  const [kinoDrag, setKinoDrag] = useState<{ x: number; y: number; heading: "left" | "right" } | null>(null);
  const [kinoLandings, setKinoLandings] = useState(0);
  const kinoDragRef = useRef<{ pointerId: number; offsetX: number; offsetY: number; startX: number; startY: number; turnX: number; heading: "left" | "right"; moved: boolean } | null>(null);
  const suppressEntryClickRef = useRef(false);
  const assistantBusy = useAssistantBusy();
  const positionStore = useMemo(createPositionStore, [videoId]);
  const [panelHovered, setPanelHovered] = useState(false);
  const [panelActivity, setPanelActivity] = useState(0);
  const { pauseOnSend, setPauseOnSend, fadeDelay, setFadeDelay } = useWatchPreferences();
  const [panelPosition, setPanelPosition] = useState<{ x: number; y: number } | null>(null);
  const panelDragRef = useRef<{ x: number; y: number } | null>(null);
  const [panelIdle, setPanelIdle] = useState(false);
  const [panelWidth, setPanelWidth] = useState(savedPanelWidth);
  const [resizing, setResizing] = useState(false);
  const [viewportWidth, setViewportWidth] = useState(window.innerWidth);
  const [viewportHeight, setViewportHeight] = useState(window.innerHeight);
  const maxPanelWidth = Math.min(850, Math.max(MIN_PANEL_WIDTH, viewportWidth - 420));
  const visiblePanelWidth = Math.min(panelWidth, maxPanelWidth);
  const compactViewport = viewportWidth <= 800;
  const effectiveMode = compactViewport ? "docked" : panelMode;
  const [pageFullscreen, setPageFullscreen] = useState(false);
  const seriesQuery = useQuery({
    queryKey: ["series-detail", seriesId, tenantId],
    queryFn: () => getSeries(seriesId!, tenantId),
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
  const rawAtSeconds = searchParams.get("atSeconds");
  const parsedAtSeconds = rawAtSeconds !== null && /^\d+$/.test(rawAtSeconds) ? Number(rawAtSeconds) : undefined;
  const explicitAtSeconds = parsedAtSeconds !== undefined && Number.isSafeInteger(parsedAtSeconds) ? parsedAtSeconds : undefined;
  const rawAtMs = searchParams.get("atMs");
  const parsedAtMs = rawAtMs !== null && /^\d+$/.test(rawAtMs) ? Number(rawAtMs) : undefined;
  const legacyAtSeconds = parsedAtMs !== undefined && Number.isSafeInteger(parsedAtMs) ? parsedAtMs : undefined;
  const atSeconds = explicitAtSeconds ?? legacyAtSeconds;

  const updatePlaybackContext = useCallback((positionSeconds: number) => {
    positionStore.set(positionSeconds);
    if (!selected) return;
    playbackContextRef.current = {
      playback: {
        seriesId: selected.seriesId,
        videoId: selected.videoId,
        episodeOrder: selected.episodeOrder,
        positionSeconds,
      },
    };
  }, [positionStore, selected?.episodeOrder, selected?.seriesId, selected?.videoId]);

  const captureExtraContext = useCallback((): Record<string, unknown> | null => {
    if (!selected) return null;
    const snapshot = pauseOnSend ? playerRef.current?.pauseAndGetSnapshot() : playerRef.current?.getSnapshot();
    if (snapshot) {
      if (pauseOnSend && snapshot.wasPlaying) resumeAfterChatRef.current = true;
      updatePlaybackContext(snapshot.positionSeconds);
    }
    const context = playbackContextRef.current;
    if (context?.playback.seriesId !== selected.seriesId
      || context.playback.videoId !== selected.videoId
      || context.playback.episodeOrder !== selected.episodeOrder) return null;
    return context;
  }, [pauseOnSend, selected, updatePlaybackContext]);

  const focusChat = () => {
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
    if (settingsRef.current) settingsRef.current.open = false;
    requestAnimationFrame(() => panelToggleRef.current?.focus());
  };

  const togglePanelMode = () => {
    setPanelMode(panelMode === "docked" ? "floating" : "docked");
  };

  const switchPaneOnTab = (event: KeyboardEvent<HTMLElement>, destination: "chat" | "player") => {
    if (event.key !== "Tab" || (event.target instanceof Element && event.target.closest('[role="search"]'))) return;
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


  useEffect(() => { setPanelPosition(null); }, [viewportWidth, viewportHeight, effectiveMode]);

  // A floating window that nobody is using fades away so the picture shows through; any touch brings it back.
  useEffect(() => {
    setPanelIdle(false);
    if (!panelOpen || effectiveMode !== "floating" || panelHovered || assistantBusy) return;
    const timer = window.setTimeout(() => setPanelIdle(true), fadeDelay * 1000);
    return () => window.clearTimeout(timer);
  }, [panelOpen, effectiveMode, panelHovered, assistantBusy, panelActivity, fadeDelay]);

  useEffect(() => {
    try { window.localStorage.setItem(KINO_DOCK_KEY, JSON.stringify(kinoDock)); } catch { /* Kino stays put for this visit. */ }
  }, [kinoDock]);

  useEffect(() => {
    try { window.localStorage.setItem(PANEL_MODE_KEY, panelMode); } catch { /* The mode still applies for this visit. */ }
  }, [panelMode]);


  useEffect(() => {
    const closeSettingsOutside = (event: globalThis.PointerEvent) => {
      const settings = settingsRef.current;
      if (settings?.open && event.target instanceof Node && !settings.contains(event.target)) settings.open = false;
    };
    document.addEventListener("pointerdown", closeSettingsOutside);
    return () => document.removeEventListener("pointerdown", closeSettingsOutside);
  }, []);

  useEffect(() => {
    const sync = () => { setViewportWidth(window.innerWidth); setViewportHeight(window.innerHeight); };
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  useLayoutEffect(() => {
    setWatchPanelLayout?.({ open: panelOpen && effectiveMode === "docked", width: visiblePanelWidth, resizing });
  }, [panelOpen, effectiveMode, visiblePanelWidth, resizing, setWatchPanelLayout]);

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
      throw new Error(t("当前浏览器不支持网页全屏"));
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

  const kinoGutter = viewportWidth <= 580 ? 16 : 24;
  const topInset = pageFullscreen ? 0 : HEADER_HEIGHT;
  const kinoBottom = clampKinoBottom(kinoDock.bottom, viewportHeight, topInset);
  const kinoPosition = kinoDrag ?? {
    x: kinoDock.side === "right" ? viewportWidth - kinoGutter - KINO_SIZE : kinoGutter,
    y: viewportHeight - kinoBottom - KINO_SIZE,
  };
  // The floating window keeps its full height and lines up with Kino as far as the viewport allows.
  const floatingHeight = Math.min(640, Math.round(viewportHeight * 0.62), viewportHeight - topInset - 40);
  const floatingBottom = Math.max(16, Math.min(kinoBottom, viewportHeight - topInset - 24 - floatingHeight));

  const startKinoDrag = (event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    kinoDragRef.current = { pointerId: event.pointerId, offsetX: event.clientX - bounds.left, offsetY: event.clientY - bounds.top, startX: event.clientX, startY: event.clientY, turnX: event.clientX, heading: kinoDock.side === "right" ? "left" : "right", moved: false };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const moveKino = (event: PointerEvent<HTMLButtonElement>) => {
    const drag = kinoDragRef.current;
    if (drag?.pointerId !== event.pointerId) return;
    if (!drag.moved && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 4) return;
    drag.moved = true;
    // Turn only after a few pixels the other way, so a shaky hand does not make Kino flicker.
    if (Math.abs(event.clientX - drag.turnX) >= 6) {
      drag.heading = event.clientX < drag.turnX ? "left" : "right";
      drag.turnX = event.clientX;
    } else if ((drag.heading === "left") === (event.clientX < drag.turnX)) drag.turnX = event.clientX;
    setKinoDrag({
      heading: drag.heading,
      x: Math.max(0, Math.min(window.innerWidth - KINO_SIZE, event.clientX - drag.offsetX)),
      y: Math.max(0, Math.min(window.innerHeight - KINO_SIZE, event.clientY - drag.offsetY)),
    });
  };

  const dropKino = (event: PointerEvent<HTMLButtonElement>) => {
    const drag = kinoDragRef.current;
    if (drag?.pointerId !== event.pointerId) return;
    kinoDragRef.current = null;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    setKinoDrag(null);
    if (!drag.moved || event.type === "pointercancel") return;
    suppressEntryClickRef.current = true;
    const left = event.clientX - drag.offsetX;
    setKinoDock({
      side: left + KINO_SIZE / 2 < window.innerWidth / 2 ? "left" : "right",
      bottom: clampKinoBottom(window.innerHeight - (event.clientY - drag.offsetY) - KINO_SIZE, window.innerHeight, topInset),
    });
    setKinoLandings((count) => count + 1);
  };

  if (!seriesId) return <main className={`page ${styles.page}`}><p role="alert">{t("视频地址无效。")}</p></main>;
  if (seriesQuery.isPending) return <main className={`page ${styles.page}`}><p className={styles.pageState} role="status"><Mascot mood="seek" size={64} delayed />{t("正在读取视频系列…")}</p></main>;
  if (seriesQuery.isError) return <main className={`page ${styles.page}`}><p className={styles.pageState} role="alert"><Mascot mood="oops" size={64} />{t("读取视频系列失败：{message}", { message: errorMessage(seriesQuery.error, locale) })}</p></main>;
  if (!series) return <main className={`page ${styles.page}`}><p className={styles.pageState} role="alert"><Mascot mood="puzzled" size={64} />{t("视频系列不存在。")}</p></main>;


  return (
    <main ref={pageRef} className={`page ${styles.page}`} data-panel-open={panelOpen} data-fullscreen={pageFullscreen} style={{ "--watch-panel-width": `${visiblePanelWidth}px` } as CSSProperties}>
      <div ref={layoutRef} className={styles.layout}>
        <section className={styles.viewer} aria-label={t("视频播放器")} onKeyDownCapture={(event) => switchPaneOnTab(event, "chat")}>
          {episodes.length === 0 ? <p className={styles.unavailable} role="status">{t("这个系列还没有视频。")}</p> : !selected ? (
            <p className={styles.unavailable} role="alert">{t("找不到这集视频，请在下方选集重新选择。")}</p>
          ) : <VideoPlayer ref={playerRef} key={`${selected.videoId}:${atSeconds ?? "resume"}`} video={selected} atSeconds={atSeconds} fullscreen={pageFullscreen} onToggleFullscreen={togglePageFullscreen} onPlaybackPositionChange={updatePlaybackContext} />}
        </section>
      </div>

      <WatchPanelPortal>
      <button ref={panelToggleRef} className={styles.kinoEntry} type="button" data-hidden={panelOpen} data-dragging={Boolean(kinoDrag)}
        style={{ left: kinoPosition.x, top: kinoPosition.y }}
        onClick={() => {
          if (suppressEntryClickRef.current) { suppressEntryClickRef.current = false; return; }
          focusChat();
        }}
        onPointerDown={startKinoDrag} onPointerMove={moveKino} onPointerUp={dropKino} onPointerCancel={dropKino}
        aria-controls="watch-side-panel" aria-expanded={panelOpen} aria-hidden={panelOpen || undefined} tabIndex={panelOpen ? -1 : undefined}
        aria-label={t("与 Kino 对话")} title={t("与 Kino 对话")}
        onPointerEnter={() => setEntryHovered(true)} onPointerLeave={() => setEntryHovered(false)}
        onFocus={() => setEntryHovered(true)} onBlur={() => setEntryHovered(false)}>
        <Kino state={kinoDrag ? (kinoDrag.heading === "left" ? "dashLeft" : "dashRight") : assistantBusy ? "loading" : entryHovered ? "look" : "idle"} width={38} scale={3} ground={false} dozeAfterMs={45_000}
          reaction={kinoLandings ? { name: "jump", key: kinoLandings } : null} />
      </button>
      <aside id="watch-side-panel" className={styles.sidePanel} aria-label={t("观看侧栏")} data-open={panelOpen} data-mode={effectiveMode} data-side={kinoDock.side} data-idle={panelIdle}
        onPointerEnter={() => setPanelHovered(true)} onPointerLeave={() => setPanelHovered(false)}
        onPointerDown={() => setPanelActivity((count) => count + 1)} onKeyDownCapture={() => setPanelActivity((count) => count + 1)}
        style={{ ...(effectiveMode === "floating" && panelPosition ? { left: panelPosition.x, top: panelPosition.y, right: "auto", bottom: "auto" } : {}), "--kino-panel-bottom": `${floatingBottom}px`, "--kino-panel-height": `${floatingHeight}px` } as CSSProperties} aria-hidden={!panelOpen} inert={!panelOpen} onKeyDown={(event) => { if (event.key === "Escape" && !event.defaultPrevented) { event.stopPropagation(); closePanel(); } }}>
        {panelOpen && effectiveMode === "docked" ? <div
          className={styles.resizeHandle}
          role="separator"
          tabIndex={0}
          aria-label={t("调整右侧栏宽度")}
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

        <div className={styles.panelToolbar}
          onPointerDown={(event) => {
            if (effectiveMode !== "floating" || event.button !== 0 || (event.target as HTMLElement).closest("button, details")) return;
            const bounds = event.currentTarget.parentElement!.getBoundingClientRect();
            panelDragRef.current = { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
            event.currentTarget.setPointerCapture(event.pointerId);
            event.preventDefault();
          }}
          onPointerMove={(event) => {
            const drag = panelDragRef.current;
            if (!drag) return;
            const bounds = event.currentTarget.parentElement!.getBoundingClientRect();
            setPanelPosition({
              x: Math.max(8, Math.min(window.innerWidth - bounds.width - 8, event.clientX - drag.x)),
              y: Math.max(8, Math.min(window.innerHeight - bounds.height - 8, event.clientY - drag.y)),
            });
          }}
          onPointerUp={(event) => {
            panelDragRef.current = null;
            if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onPointerCancel={() => { panelDragRef.current = null; }}
          onLostPointerCapture={() => { panelDragRef.current = null; }}
        >
          <div className={styles.sideTabs} role="tablist" aria-label={t("观看侧栏内容")}>
            <button type="button" role="tab" id="watch-chat-tab" aria-controls="watch-chat-panel" aria-selected={panelTab === "chat"} onClick={focusChat}><MessageCircle size={15} aria-hidden="true" />{t("聊天")}</button>
            <button type="button" role="tab" id="watch-episodes-tab" aria-controls="watch-episodes-panel" aria-selected={panelTab === "episodes"} onClick={() => setPanelTab("episodes")}><ListVideo size={15} aria-hidden="true" />{t("选集")}</button>
          </div>
          <details ref={settingsRef} className={styles.watchSettings} onKeyDown={(event) => {
            if (event.key !== "Escape" || !event.currentTarget.open) return;
            event.preventDefault();
            event.stopPropagation();
            event.currentTarget.open = false;
            event.currentTarget.querySelector("summary")?.focus();
          }}>
            <summary role="button" aria-label={t("观看设置")} title={t("观看设置")}><Settings2 size={16} aria-hidden="true" /></summary>
            <div className={styles.watchSettingsPopover}>
              <label className={styles.fadeDelay}>
                <span>{t("自动淡化时间")}</span>
                <span className={styles.fadeDelayControl}>
                  <input type="number" min={1} step="any" defaultValue={fadeDelay} key={fadeDelay} onBlur={(event) => {
                    const seconds = event.currentTarget.valueAsNumber;
                    if (Number.isFinite(seconds) && seconds >= 1) setFadeDelay(seconds);
                    else event.currentTarget.value = String(fadeDelay);
                  }} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }}
                    aria-label={t("自动淡化时间（秒）")} />
                  <span>{t("秒")}</span>
                </span>
              </label>
              <label className={styles.pauseOnSend}>
                <span>{t("发送问题时暂停视频")}</span>
                <input type="checkbox" role="switch" checked={pauseOnSend} onChange={(event) => setPauseOnSend(event.target.checked)} />
              </label>
            </div>
          </details>
          {!compactViewport ? <button className={styles.panelClose} type="button" onClick={togglePanelMode}
            aria-label={panelMode === "floating" ? t("停靠到侧栏") : t("改为悬浮窗")} title={panelMode === "floating" ? t("停靠到侧栏") : t("改为悬浮窗")}>
            {panelMode === "floating" ? <Maximize2 size={16} aria-hidden="true" /> : <Minimize2 size={16} aria-hidden="true" />}
          </button> : null}
          <button className={styles.panelClose} type="button" onClick={closePanel} aria-label={t("关闭对话面板")}><X size={18} aria-hidden="true" /></button>
        </div>
        <div
          id="watch-chat-panel"
          className={styles.chatPanel}
          role="tabpanel"
          aria-labelledby="watch-chat-tab"
          hidden={panelTab !== "chat"}
          onKeyDownCapture={(event) => switchPaneOnTab(event, "player")}
        >
          <SearchPage ref={chatRef} embedded initialSeriesId={seriesId} initialSeries={series} captureExtraContext={captureExtraContext}
            composerContext={selected ? <PlaybackContextChip store={positionStore} episodeOrder={selected.episodeOrder} /> : null} onOpenVideo={({ tenantId: targetTenantId, seriesId: targetSeriesId, videoId: targetVideoId, startSeconds }) => {
            navigate(`${watchPath({ tenantId: targetTenantId, seriesId: targetSeriesId }, targetVideoId)}&atSeconds=${startSeconds}`);
          }} onSeekCurrent={selected ? (positionMs) => {
            navigate(`${watchPath(series, selected.videoId)}&atSeconds=${positionMs}`);
          } : undefined} />
        </div>
        <div id="watch-episodes-panel" className={styles.episodePanel} role="tabpanel" aria-labelledby="watch-episodes-tab" hidden={panelTab !== "episodes"}>
          <h2>{t("选集")} <span><small>{t("{count} 集", { count: episodes.length })}</small>{canManage && series.access === "member" ? <Link to="/library">{t("调整集数")}</Link> : null}</span></h2>
          <ol className={styles.episodeList}>
            {episodes.map((episode) => (
              <li key={episode.videoId}>
                <EpisodeLink video={episode} to={watchPath(series, episode.videoId)} current={episode.videoId === selected?.videoId}>
                  <span>{episodeLabel(episode.episodeOrder)}</span>
                  <strong title={episode.originalFilename ?? undefined}>{videoTitle(episode, locale)}</strong>
                  <small>{formatTimecode(episode.durationSeconds)} · {episode.playbackStatus === "READY" ? t("可播放") : t("播放未就绪")}</small>
                </EpisodeLink>
              </li>
            ))}
          </ol>
        </div>
      </aside>
      </WatchPanelPortal>
      <div className={styles.watchDetails}>
        <section className={styles.watchSynopsis} aria-label={t("视频简介")}>
          <Link className={styles.seriesName} to={seriesPath(series)}>{seriesTitle(series)}<ChevronRight size={18} aria-hidden="true" /></Link>
          <h1 title={selected?.originalFilename ?? undefined}>{selected ? videoTitle(selected, locale) : seriesTitle(series)}</h1>
          <div className={styles.videoMeta}><span>{selected ? formatTimecode(selected.durationSeconds) : null}</span><span>{t("共 {count} 集", { count: episodes.length })}</span></div>
          {selected?.description || series.description ? <p>{selected?.description || series.description}</p> : null}
          {selected ? <nav className={styles.neighbors} aria-label={t("切换集数")}>
            {selectedIndex > 0 ? <EpisodeLink video={episodes[selectedIndex - 1]!} to={watchPath(series, episodes[selectedIndex - 1]!.videoId)}><ChevronLeft size={16} />{t("上一集")}</EpisodeLink> : null}
            {selectedIndex < episodes.length - 1 ? <EpisodeLink video={episodes[selectedIndex + 1]!} to={watchPath(series, episodes[selectedIndex + 1]!.videoId)}>{t("下一集")}<ChevronRight size={16} /></EpisodeLink> : null}
          </nav> : null}
        </section>
        <section className={styles.upNext} aria-label={t("后续集数")}>
          <h2>{followingEpisodes.length ? t("接下来") : t("本系列选集")}</h2>
          <ol className={styles.nextEpisodes}>
            {suggestedEpisodes.map((episode) => <li key={episode.videoId}>
              <EpisodeLink video={episode} to={watchPath(series, episode.videoId)} current={episode.videoId === selected?.videoId}>
                <span className={styles.nextPreview} aria-hidden="true">
                  <Clapperboard size={24} strokeWidth={1} />
                  {episode.thumbnail ? <img src={mediaUrl(episode.thumbnail.url) ?? undefined} alt="" loading="lazy" onError={(event) => { event.currentTarget.hidden = true; }} />
                    : episode.playbackStatus === "READY" && episode.playbackUrl ? <PlaybackFrame className={styles.seriesFrame} src={mediaUrl(episode.playbackUrl)} positionSeconds={previewFramePosition(episode.durationSeconds)} /> : null}
                  <span className={styles.nextPlay}><Play size={17} fill="currentColor" /></span>
                </span>
                <span className={styles.nextTitle}><small>{episodeLabel(episode.episodeOrder)}{episode.videoId === selected?.videoId ? t(" · 当前集") : ""}</small><strong title={episode.originalFilename ?? undefined}>{videoTitle(episode, locale)}</strong><span>{formatTimecode(episode.durationSeconds)}{episode.playbackStatus !== "READY" ? t(" · 播放未就绪") : ""}</span></span>
              </EpisodeLink>
            </li>)}
          </ol>
        </section>
      </div>
    </main>
  );
}
