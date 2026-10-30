import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef, StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureDataSource, PRIMARY_CONNECTION } from "../../api/client";
import type { AuthIdentity, DataSource, VideoSummary } from "../../api/contracts";
import { playbackProgressStorageKey, readVideoProgress, readWatchHistory, saveVideoProgress } from "./playbackProgress";
import { VideoPlayer, type VideoPlayerHandle } from "./VideoPlayer";

function video(videoId = "video-1", playbackStatus: VideoSummary["playbackStatus"] = "READY"): VideoSummary {
  return {
    videoId, tenantId: "local-default", seriesId: "series-1", episodeOrder: 1, uploadSequence: 1,
    originalFilename: `${videoId}.mp4`, totalBytes: 100, durationSeconds: 120_000,
    uploadStatus: "COMPLETED", parseStatus: "SUCCEEDED", uploadProgress: 100, parseProgress: 100,
    indexStatus: "READY", playbackStatus,
    playbackUrl: playbackStatus === "READY" ? `/api/videos/content?videoId=${videoId}` : null,
    playbackUrlExpiresAt: null, thumbnail: null, latestJobId: null, lastError: null,
  };
}

function metadata(media: HTMLVideoElement) {
  Object.defineProperty(media, "duration", { value: 120, configurable: true });
  fireEvent.loadedMetadata(media);
}

const localIdentity: AuthIdentity = {
  userId: "local-user", email: null, isGuest: false, tenantId: "local-default",
  publicTenantId: "public-courses", mode: "local", csrfToken: "",
  permissions: { canUpload: true, canManage: true, canConfigure: true },
};
const localSource: DataSource = {
  key: `${PRIMARY_CONNECTION.id}:local`, kind: "local", connection: PRIMARY_CONNECTION,
  tenantId: "local-default", identity: localIdentity,
};

function personalSource(userId: string): DataSource {
  return {
    key: "remote:personal", kind: "personal", connection: { id: "remote", baseUrl: "/remote" },
    tenantId: "personal-tenant", identity: {
      ...localIdentity, userId, email: `${userId}@example.com`, mode: "cloud",
      tenantId: "personal-tenant", csrfToken: `${userId}-csrf`,
    },
  };
}

let play: ReturnType<typeof vi.spyOn>;
let pause: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  localStorage.clear();
  configureDataSource(localSource);
  play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});
afterEach(() => { cleanup(); configureDataSource(localSource); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("VideoPlayer", () => {
  it("hides controls five seconds after leaving the whole player and cancels hiding on return", () => {
    vi.useFakeTimers();
    render(<VideoPlayer video={video()} />);
    const shell = screen.getByLabelText("视频播放器");
    const media = screen.getByLabelText("EP 01 视频");
    const timeline = screen.getByRole("slider", { name: "播放进度" });
    fireEvent.pointerEnter(shell);
    act(() => vi.advanceTimersByTime(6_000));
    expect(shell).toHaveAttribute("data-controls-visible", "true");
    fireEvent.pointerOut(media, { relatedTarget: timeline });
    fireEvent.pointerOver(timeline, { relatedTarget: media });
    act(() => vi.advanceTimersByTime(6_000));
    expect(shell).toHaveAttribute("data-controls-visible", "true");

    fireEvent.pointerLeave(shell);
    act(() => vi.advanceTimersByTime(4_999));
    expect(shell).toHaveAttribute("data-controls-visible", "true");
    act(() => vi.advanceTimersByTime(1));
    expect(shell).toHaveAttribute("data-controls-visible", "false");
    fireEvent.pointerEnter(shell);
    expect(shell).toHaveAttribute("data-controls-visible", "true");
    fireEvent.pointerLeave(shell);
    act(() => vi.advanceTimersByTime(4_000));
    fireEvent.pointerEnter(shell);
    act(() => vi.advanceTimersByTime(2_000));
    expect(shell).toHaveAttribute("data-controls-visible", "true");
  });

  it("reveals controls for keyboard focus without letting mouse focus prevent hiding", () => {
    vi.useFakeTimers();
    render(<VideoPlayer video={video()} />);
    const shell = screen.getByLabelText("视频播放器");
    const playButton = screen.getByRole("button", { name: /^播放$/ });
    act(() => vi.advanceTimersByTime(5_000));
    expect(shell).toHaveAttribute("data-controls-visible", "false");
    act(() => playButton.focus());
    act(() => vi.advanceTimersByTime(6_000));
    expect(shell).toHaveAttribute("data-controls-visible", "true");
    fireEvent.pointerEnter(shell);
    fireEvent.pointerDown(playButton);
    fireEvent.pointerUp(playButton);
    fireEvent.pointerLeave(shell);
    act(() => vi.advanceTimersByTime(5_000));
    expect(playButton).toHaveFocus();
    expect(shell).toHaveAttribute("data-controls-visible", "false");
    fireEvent.keyDown(playButton, { key: "Tab" });
    expect(shell).toHaveAttribute("data-controls-visible", "true");
    act(() => playButton.blur());
    act(() => vi.advanceTimersByTime(5_000));
    expect(shell).toHaveAttribute("data-controls-visible", "false");
  });

  it.each(["pointerUp", "pointerCancel"] as const)("keeps controls visible during a drag outside until %s", (release) => {
    vi.useFakeTimers();
    render(<VideoPlayer video={video()} />);
    const shell = screen.getByLabelText("视频播放器");
    metadata(screen.getByLabelText<HTMLVideoElement>("EP 01 视频"));
    const timeline = screen.getByRole("slider", { name: "播放进度" });
    fireEvent.pointerEnter(shell);
    fireEvent.pointerDown(timeline);
    fireEvent.pointerLeave(shell);
    act(() => vi.advanceTimersByTime(6_000));
    expect(shell).toHaveAttribute("data-controls-visible", "true");
    fireEvent[release](window);
    act(() => vi.advanceTimersByTime(4_999));
    expect(shell).toHaveAttribute("data-controls-visible", "true");
    act(() => vi.advanceTimersByTime(1));
    expect(shell).toHaveAttribute("data-controls-visible", "false");
  });

  it("restarts the visibility delay for a new video and clears it on unmount", () => {
    vi.useFakeTimers();
    const view = render(<VideoPlayer video={video()} />);
    const shell = screen.getByLabelText("视频播放器");
    act(() => vi.advanceTimersByTime(4_000));
    view.rerender(<VideoPlayer video={video("video-2")} />);
    act(() => vi.advanceTimersByTime(1_000));
    expect(shell).toHaveAttribute("data-controls-visible", "true");
    act(() => vi.advanceTimersByTime(4_000));
    expect(shell).toHaveAttribute("data-controls-visible", "false");
    fireEvent.pointerEnter(shell);
    fireEvent.pointerLeave(shell);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps millisecond duration and seek precision while displaying whole seconds", () => {
    const onPlaybackPositionChange = vi.fn();
    render(<VideoPlayer video={{ ...video(), durationSeconds: 8_008 }} atSeconds={3_456} onPlaybackPositionChange={onPlaybackPositionChange} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    Object.defineProperty(media, "duration", { value: 8.008, configurable: true });
    fireEvent.loadedMetadata(media);
    expect(media.currentTime).toBe(3.456);
    expect(onPlaybackPositionChange).toHaveBeenLastCalledWith(3_456);
    expect(screen.getByRole("slider", { name: "播放进度" })).toHaveAttribute("max", "8008");
    expect(screen.getByText("00:03 / 00:08")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("slider", { name: "播放进度" }), { target: { value: "4567" } });
    expect(media.currentTime).toBe(4.567);
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(4_567);
  });

  it("initializes cached metadata without waiting for another loadedmetadata event", () => {
    vi.spyOn(HTMLMediaElement.prototype, "readyState", "get").mockReturnValue(HTMLMediaElement.HAVE_ENOUGH_DATA);
    vi.spyOn(HTMLMediaElement.prototype, "duration", "get").mockReturnValue(12.012);
    const ref = createRef<VideoPlayerHandle>();
    const view = render(<VideoPlayer ref={ref} video={{ ...video(), durationSeconds: 12_012 }} atSeconds={2_345} />, { wrapper: StrictMode });
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");

    expect(screen.getByRole("slider", { name: "播放进度" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "前进 10 秒" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "后退 10 秒" })).toBeEnabled();
    expect(media.currentTime).toBe(2.345);
    expect(ref.current?.getSnapshot()?.positionSeconds).toBe(2_345);
    fireEvent.change(screen.getByRole("slider", { name: "播放进度" }), { target: { value: "10260" } });
    expect(media.currentTime).toBe(10.26);
    fireEvent.click(screen.getByRole("button", { name: "后退 10 秒" }));
    expect(media.currentTime).toBeCloseTo(0.26);

    view.rerender(<VideoPlayer ref={ref} video={{ ...video("video-2"), durationSeconds: 12_012 }} atSeconds={0} />);
    expect(screen.getByLabelText<HTMLVideoElement>("EP 01 视频")).not.toBe(media);
    expect(ref.current?.getSnapshot()?.positionSeconds).toBe(0);
    expect(screen.getByRole("slider", { name: "播放进度" })).toBeEnabled();
  });

  it("keeps loaded media ready and its position when server duration is refreshed", () => {
    const ref = createRef<VideoPlayerHandle>();
    const view = render(<VideoPlayer ref={ref} video={{ ...video(), durationSeconds: 0 }} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    metadata(media);
    media.currentTime = 118;
    fireEvent.timeUpdate(media);

    view.rerender(<VideoPlayer ref={ref} video={video()} />);

    expect(screen.getByLabelText("EP 01 视频")).toBe(media);
    expect(screen.getByRole("slider", { name: "播放进度" })).toBeEnabled();
    expect(screen.getByRole("slider", { name: "播放进度" })).toHaveValue("118000");
    expect(ref.current?.getSnapshot()?.positionSeconds).toBe(118_000);
    expect(media.currentTime).toBe(118);
    fireEvent.click(screen.getByRole("button", { name: "后退 10 秒" }));
    expect(media.currentTime).toBe(108);
  });

  it("seeks to explicit atSeconds before saved progress, including an explicit zero", () => {
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionSeconds: 30_000, durationSeconds: 120_000 });
    const view = render(<VideoPlayer video={video()} atSeconds={20_000} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    expect(media).toHaveAttribute("preload", "metadata");
    expect(media).toHaveAttribute("playsinline");
    metadata(media);
    expect(media.currentTime).toBe(20);
    view.rerender(<VideoPlayer video={video()} atSeconds={0} />);
    const next = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    metadata(next);
    expect(next.currentTime).toBe(0);
  });

  it("resumes the current video but restarts when saved progress is near its end", () => {
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionSeconds: 36_000, durationSeconds: 120_000 });
    saveVideoProgress({ seriesId: "series-1", videoId: "video-2", positionSeconds: 118_000, durationSeconds: 120_000 });
    const view = render(<VideoPlayer video={video()} />);
    metadata(screen.getByLabelText<HTMLVideoElement>("EP 01 视频"));
    expect(screen.getByLabelText<HTMLVideoElement>("EP 01 视频").currentTime).toBe(36);
    view.rerender(<VideoPlayer video={video("video-2")} />);
    const next = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    metadata(next);
    expect(next.currentTime).toBe(0);
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(36_000);
  });

  it("publishes metadata, time update, and seek positions independently from progress persistence", () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000);
    const onPlaybackPositionChange = vi.fn();
    render(<VideoPlayer video={video()} atSeconds={1_000} onPlaybackPositionChange={onPlaybackPositionChange} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");

    metadata(media);
    expect(onPlaybackPositionChange).toHaveBeenLastCalledWith(1_000);

    media.currentTime = 2;
    fireEvent.timeUpdate(media);
    media.currentTime = 2.5;
    fireEvent.timeUpdate(media);
    expect(onPlaybackPositionChange).toHaveBeenLastCalledWith(2_500);
    expect(readVideoProgress("video-1")).toBeNull();

    media.currentTime = 3.456;
    fireEvent.seeked(media);
    expect(onPlaybackPositionChange).toHaveBeenLastCalledWith(3_456);
    expect(readVideoProgress("video-1")).toBeNull();
    expect(onPlaybackPositionChange).toHaveBeenCalledTimes(4);
  });

  it("controls playback, seeking, volume, speed, fullscreen, and keyboard shortcuts", () => {
    const toggleFullscreen = vi.fn();
    render(<VideoPlayer video={video()} onToggleFullscreen={toggleFullscreen} />);
    const shell = screen.getByLabelText<HTMLElement>("视频播放器");
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    metadata(media);
    fireEvent.click(screen.getByRole("button", { name: /^播放$/ }));
    expect(play).toHaveBeenCalledOnce();
    fireEvent.play(media);
    Object.defineProperty(media, "paused", { value: false, configurable: true });
    fireEvent.click(screen.getByRole("button", { name: "暂停" }));
    expect(pause).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole("button", { name: "前进 10 秒" }));
    expect(media.currentTime).toBe(10);
    fireEvent.change(screen.getByRole("slider", { name: "播放进度" }), { target: { value: "45000" } });
    expect(media.currentTime).toBe(45);
    fireEvent.keyDown(shell, { key: "ArrowLeft" });
    expect(media.currentTime).toBe(35);
    fireEvent.click(screen.getByRole("button", { name: "静音" }));
    expect(media.muted).toBe(true);
    fireEvent.change(screen.getByRole("slider", { name: "音量" }), { target: { value: "0.4" } });
    expect(media.volume).toBe(0.4);
    fireEvent.change(screen.getByRole("combobox", { name: "播放倍速" }), { target: { value: "1.5" } });
    expect(media.playbackRate).toBe(1.5);
    fireEvent.click(screen.getByRole("button", { name: "网页全屏" }));
    fireEvent.keyDown(shell, { key: "f" });
    expect(toggleFullscreen).toHaveBeenCalledTimes(2);
  });

  it("writes progress at most every five seconds and flushes on pause, page leave, and unmount", () => {
    const clock = vi.spyOn(Date, "now").mockReturnValue(1_000);
    const view = render(<VideoPlayer video={video()} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    metadata(media);
    media.currentTime = 2; fireEvent.timeUpdate(media);
    expect(readVideoProgress("video-1")).toBeNull();
    clock.mockReturnValue(6_000);
    media.currentTime = 6; fireEvent.timeUpdate(media);
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(6_000);
    clock.mockReturnValue(7_000);
    media.currentTime = 7; fireEvent.timeUpdate(media);
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(6_000);
    fireEvent.pause(media);
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(7_000);
    clock.mockReturnValue(8_000);
    media.currentTime = 8; fireEvent.timeUpdate(media);
    window.dispatchEvent(new Event("pagehide"));
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(8_000);
    view.unmount();
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(8_000);
  });

  it("keeps legacy local reads and final writes local after the active source changes", () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000);
    localStorage.setItem("seekino-watch-progress:v2", JSON.stringify([
      { seriesId: "series-1", videoId: "video-1", positionSeconds: 12, durationSeconds: 120, updatedAt: 100 },
    ]));
    const localKey = playbackProgressStorageKey();
    const view = render(<VideoPlayer video={video()} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    act(() => {
      configureDataSource(personalSource("user-a"));
      metadata(media);
      expect(media.currentTime).toBe(12);
      expect(localStorage.getItem("seekino-watch-progress:v2")).toBeNull();
      expect(readWatchHistory()).toEqual([]);

      media.currentTime = 18;
      fireEvent.timeUpdate(media);
      window.dispatchEvent(new Event("pagehide"));
      expect(readVideoProgress("video-1", localKey)?.positionSeconds).toBe(18_000);
      expect(readWatchHistory()).toEqual([]);
      media.currentTime = 19;
      fireEvent.timeUpdate(media);
      view.unmount();
    });
    expect(readVideoProgress("video-1", localKey)?.positionSeconds).toBe(19_000);
    expect(readWatchHistory()).toEqual([]);
    expect(localStorage.getItem(playbackProgressStorageKey())).toBeNull();
  });

  it.each([false, true])("preserves separate progress for equal video IDs when switching accounts before unmount (cached=%s)", (cached) => {
    if (cached) {
      vi.spyOn(HTMLMediaElement.prototype, "readyState", "get").mockReturnValue(HTMLMediaElement.HAVE_ENOUGH_DATA);
      vi.spyOn(HTMLMediaElement.prototype, "duration", "get").mockReturnValue(120);
    }
    configureDataSource(personalSource("user-a"));
    const firstKey = playbackProgressStorageKey();
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionSeconds: 12_000, durationSeconds: 120_000 });
    configureDataSource(personalSource("user-b"));
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionSeconds: 70_000, durationSeconds: 120_000 });
    configureDataSource(personalSource("user-a"));
    const view = render(<VideoPlayer video={video()} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    if (!cached) metadata(media);
    expect(media.currentTime).toBe(12);

    configureDataSource(personalSource("user-b"));
    // The existing player remains bound to user A until the identity scope
    // remounts it, even if a final server-duration update arrives first.
    view.rerender(<VideoPlayer video={{ ...video(), durationSeconds: 119_999 }} />);
    expect(screen.getByLabelText("EP 01 视频")).toBe(media);
    expect(screen.getByRole("slider", { name: "播放进度" })).toBeEnabled();
    expect(media.currentTime).toBe(12);
    media.currentTime = 32;
    fireEvent.timeUpdate(media);
    fireEvent.pause(media);
    view.unmount();
    expect(readVideoProgress("video-1", firstKey)?.positionSeconds).toBe(32_000);
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(70_000);

    render(<VideoPlayer video={video()} />);
    const next = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    if (!cached) metadata(next);
    expect(next.currentTime).toBe(70);
  });

  it.each([true, false])("reads a fresh snapshot without changing playback or saving progress when paused=%s", (paused) => {
    const ref = createRef<VideoPlayerHandle>();
    const onPlaybackPositionChange = vi.fn();
    render(<VideoPlayer ref={ref} video={video()} onPlaybackPositionChange={onPlaybackPositionChange} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    Object.defineProperty(media, "paused", { value: paused, configurable: true });

    expect(ref.current?.getSnapshot()).toBeNull();
    expect(onPlaybackPositionChange).not.toHaveBeenCalled();
    expect(readVideoProgress("video-1")).toBeNull();

    metadata(media);
    onPlaybackPositionChange.mockClear();
    media.currentTime = 12.3456;
    expect(ref.current?.getSnapshot()).toEqual({ positionSeconds: 12_346, wasPlaying: !paused });
    media.currentTime = 91.2344;
    expect(ref.current?.getSnapshot()).toEqual({ positionSeconds: 91_234, wasPlaying: !paused });

    expect(media.currentTime).toBe(91.2344);
    expect(media.paused).toBe(paused);
    expect(pause).not.toHaveBeenCalled();
    expect(play).not.toHaveBeenCalled();
    expect(onPlaybackPositionChange).not.toHaveBeenCalled();
    expect(screen.getByRole("slider", { name: "播放进度" })).toHaveValue("0");
    expect(readVideoProgress("video-1")).toBeNull();
  });

  it("pauses and reads the integer media position through its imperative handle", () => {
    const ref = createRef<VideoPlayerHandle>();
    const onPlaybackPositionChange = vi.fn();
    render(<VideoPlayer ref={ref} video={video()} onPlaybackPositionChange={onPlaybackPositionChange} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");

    let snapshot: ReturnType<VideoPlayerHandle["pauseAndGetSnapshot"]> = null;
    act(() => { snapshot = ref.current?.pauseAndGetSnapshot() ?? null; });
    expect(snapshot).toBeNull();
    expect(pause).not.toHaveBeenCalled();
    expect(onPlaybackPositionChange).not.toHaveBeenCalled();
    expect(readVideoProgress("video-1")).toBeNull();

    metadata(media);
    media.currentTime = 12.3456;

    act(() => { snapshot = ref.current?.pauseAndGetSnapshot() ?? null; });
    expect(snapshot).toEqual({ positionSeconds: 12_346, wasPlaying: false });
    expect(pause).toHaveBeenCalledOnce();
    expect(onPlaybackPositionChange).toHaveBeenLastCalledWith(12_346);
    expect(readVideoProgress("video-1")?.positionSeconds).toBe(12_346);
  });

  it("reports whether Chat interrupted playback, focuses the player, and resumes on request", () => {
    const ref = createRef<VideoPlayerHandle>();
    render(<VideoPlayer ref={ref} video={video()} />);
    const shell = screen.getByLabelText<HTMLElement>("视频播放器");
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    metadata(media);
    media.currentTime = 21.25;
    Object.defineProperty(media, "paused", { value: false, configurable: true });

    let snapshot: ReturnType<VideoPlayerHandle["pauseAndGetSnapshot"]> = null;
    act(() => { snapshot = ref.current?.pauseAndGetSnapshot() ?? null; });
    expect(snapshot).toEqual({ positionSeconds: 21_250, wasPlaying: true });
    expect(pause).toHaveBeenCalledOnce();
    ref.current?.focus();
    expect(shell).toHaveFocus();
    Object.defineProperty(media, "paused", { value: true, configurable: true });
    act(() => { ref.current?.resume(); });
    expect(play).toHaveBeenCalledOnce();

    act(() => { snapshot = ref.current?.pauseAndGetSnapshot() ?? null; });
    expect(snapshot).toEqual({ positionSeconds: 21_250, wasPlaying: false });
  });

  it("explains unavailable media and media errors", () => {
    const view = render(<VideoPlayer video={video("video-1", "MISSING")} />);
    expect(screen.getByRole("status")).toHaveTextContent("原视频文件已丢失");
    expect(screen.queryByLabelText("EP 01 视频")).not.toBeInTheDocument();
    view.rerender(<VideoPlayer video={video()} />);
    fireEvent.error(screen.getByLabelText("EP 01 视频"));
    expect(screen.getByRole("alert")).toHaveTextContent("视频媒体无法读取");
  });
});
