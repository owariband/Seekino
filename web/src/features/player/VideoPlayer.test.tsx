import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { VideoSummary } from "../../api/contracts";
import { readVideoProgress, saveVideoProgress } from "./playbackProgress";
import { VideoPlayer, type VideoPlayerHandle } from "./VideoPlayer";

function video(videoId = "video-1", playbackStatus: VideoSummary["playbackStatus"] = "READY"): VideoSummary {
  return {
    videoId, workspaceId: "local-default", seriesId: "series-1", episodeOrder: 1, uploadSequence: 1,
    originalFilename: `${videoId}.mp4`, totalBytes: 100, durationMs: 120_000,
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

let play: ReturnType<typeof vi.spyOn>;
let pause: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  localStorage.clear();
  play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("VideoPlayer", () => {
  it("seeks to explicit atMs before saved progress, including an explicit zero", () => {
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionMs: 30_000, durationMs: 120_000 });
    const view = render(<VideoPlayer video={video()} atMs={20_500} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    expect(media).toHaveAttribute("preload", "metadata");
    expect(media).toHaveAttribute("playsinline");
    metadata(media);
    expect(media.currentTime).toBe(20.5);
    view.rerender(<VideoPlayer video={video()} atMs={0} />);
    const next = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    metadata(next);
    expect(next.currentTime).toBe(0);
  });

  it("resumes the current video but restarts when saved progress is near its end", () => {
    saveVideoProgress({ seriesId: "series-1", videoId: "video-1", positionMs: 36_000, durationMs: 120_000 });
    saveVideoProgress({ seriesId: "series-1", videoId: "video-2", positionMs: 118_000, durationMs: 120_000 });
    const view = render(<VideoPlayer video={video()} />);
    metadata(screen.getByLabelText<HTMLVideoElement>("EP 01 视频"));
    expect(screen.getByLabelText<HTMLVideoElement>("EP 01 视频").currentTime).toBe(36);
    view.rerender(<VideoPlayer video={video("video-2")} />);
    const next = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");
    metadata(next);
    expect(next.currentTime).toBe(0);
    expect(readVideoProgress("video-1")?.positionMs).toBe(36_000);
  });

  it("publishes metadata, time update, and seek positions independently from progress persistence", () => {
    vi.spyOn(Date, "now").mockReturnValue(1_000);
    const onPlaybackPositionChange = vi.fn();
    render(<VideoPlayer video={video()} atMs={1_500} onPlaybackPositionChange={onPlaybackPositionChange} />);
    const media = screen.getByLabelText<HTMLVideoElement>("EP 01 视频");

    metadata(media);
    expect(onPlaybackPositionChange).toHaveBeenLastCalledWith(1_500);

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
    expect(readVideoProgress("video-1")?.positionMs).toBe(6_000);
    clock.mockReturnValue(7_000);
    media.currentTime = 7; fireEvent.timeUpdate(media);
    expect(readVideoProgress("video-1")?.positionMs).toBe(6_000);
    fireEvent.pause(media);
    expect(readVideoProgress("video-1")?.positionMs).toBe(7_000);
    clock.mockReturnValue(8_000);
    media.currentTime = 8; fireEvent.timeUpdate(media);
    window.dispatchEvent(new Event("pagehide"));
    expect(readVideoProgress("video-1")?.positionMs).toBe(8_000);
    view.unmount();
    expect(readVideoProgress("video-1")?.positionMs).toBe(8_000);
  });

  it("pauses and reads the precise media position through its imperative handle", () => {
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
    expect(snapshot).toEqual({ positionMs: 12_346, wasPlaying: false });
    expect(pause).toHaveBeenCalledOnce();
    expect(onPlaybackPositionChange).toHaveBeenLastCalledWith(12_346);
    expect(readVideoProgress("video-1")?.positionMs).toBe(12_346);
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
    expect(snapshot).toEqual({ positionMs: 21_250, wasPlaying: true });
    expect(pause).toHaveBeenCalledOnce();
    ref.current?.focus();
    expect(shell).toHaveFocus();
    Object.defineProperty(media, "paused", { value: true, configurable: true });
    act(() => { ref.current?.resume(); });
    expect(play).toHaveBeenCalledOnce();

    act(() => { snapshot = ref.current?.pauseAndGetSnapshot() ?? null; });
    expect(snapshot).toEqual({ positionMs: 21_250, wasPlaying: false });
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
