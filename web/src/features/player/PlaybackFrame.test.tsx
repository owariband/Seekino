import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PlaybackFrame } from "./PlaybackFrame";

let intersect: (visible: boolean) => void;
let disconnect: ReturnType<typeof vi.fn>;
let play: ReturnType<typeof vi.spyOn>;
let load: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  disconnect = vi.fn();
  vi.stubGlobal("IntersectionObserver", class {
    constructor(callback: IntersectionObserverCallback) {
      intersect = (isIntersecting) => callback([{ isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
    }
    observe() {}
    disconnect = disconnect;
  });
  play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  load = vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
});
afterEach(() => {
  cleanup();
  expect(play).not.toHaveBeenCalled();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function metadata(media: HTMLVideoElement, duration = 120) {
  Object.defineProperty(media, "duration", { value: duration, configurable: true });
  fireEvent.loadedMetadata(media);
}

function ready(media: HTMLVideoElement) {
  Object.defineProperty(media, "readyState", { value: 2, configurable: true });
  fireEvent.seeked(media);
}

describe("PlaybackFrame", () => {
  it("does not request a video until its wrapper enters the viewport", () => {
    const view = render(<PlaybackFrame src="/first.mp4" positionSeconds={30_000} poster="/poster.jpg" />);
    expect(view.container.querySelector("video")).toBeNull();
    expect(load).not.toHaveBeenCalled();
    act(() => intersect(false));
    expect(view.container.querySelector("video")).toBeNull();

    act(() => intersect(true));
    expect(view.container.querySelector("video")).toHaveAttribute("src", "/first.mp4");
    expect(load).toHaveBeenCalledOnce();
    expect(disconnect).toHaveBeenCalled();
  });

  it("seeks using milliseconds and reveals a paused frame only when decoding is ready", () => {
    const view = render(<PlaybackFrame src="/first.mp4" positionSeconds={3_456} poster="/poster.jpg" eager />);
    const media = view.container.querySelector("video")!;
    const poster = view.container.querySelector("img")!;
    expect(media).not.toBeVisible();
    expect(poster).toBeVisible();
    expect(media).toHaveAttribute("preload", "metadata");
    expect(media).toHaveAttribute("playsinline");
    expect(media.muted).toBe(true);
    expect(media.autoplay).toBe(false);
    metadata(media, 8.008);
    expect(media.currentTime).toBe(3.456);
    expect(media).not.toBeVisible();

    Object.defineProperty(media, "seeking", { value: true, configurable: true });
    ready(media);
    expect(media).not.toBeVisible();
    Object.defineProperty(media, "seeking", { value: false, configurable: true });
    ready(media);
    expect(media).toBeVisible();
    expect(poster).not.toBeVisible();
  });

  it("shows the zero frame on loadeddata without requiring a seeked event", () => {
    const view = render(<PlaybackFrame src="/first.mp4" positionSeconds={0} eager />);
    const media = view.container.querySelector("video")!;
    metadata(media);
    expect(media.currentTime).toBe(0);
    expect(media).not.toBeVisible();
    Object.defineProperty(media, "readyState", { value: 2, configurable: true });
    fireEvent.loadedData(media);
    expect(media).toBeVisible();
  });

  it("clamps progress to a decodable point before the media ends", () => {
    const view = render(<PlaybackFrame src="/first.mp4" positionSeconds={120_000} eager />);
    const media = view.container.querySelector("video")!;
    metadata(media, 90);
    expect(media.currentTime).toBeCloseTo(89.9);
  });

  it("releases a failed source and keeps the poster available", () => {
    const view = render(<PlaybackFrame src="/first.mp4" positionSeconds={3_000} poster="/poster.jpg" eager />);
    const media = view.container.querySelector("video")!;
    fireEvent.error(media);
    expect(view.container.querySelector("video")).toBeNull();
    expect(media).not.toHaveAttribute("src");
    expect(view.container.querySelector("img")).toBeVisible();
    expect(load).toHaveBeenCalledTimes(2);
    fireEvent.error(view.container.querySelector("img")!);
    expect(view.container.querySelector("img")).not.toBeVisible();
  });

  it.each([
    { src: "/second.mp4", positionSeconds: 3_000 },
    { src: "/first.mp4", positionSeconds: 20_000 },
  ])("never exposes an old frame after changing to $src at $positionSeconds", (next) => {
    const view = render(<PlaybackFrame src="/first.mp4" positionSeconds={3_000} poster="/poster.jpg" eager />);
    const previous = view.container.querySelector("video")!;
    metadata(previous);
    ready(previous);
    expect(previous).toBeVisible();

    view.rerender(<PlaybackFrame {...next} poster="/poster.jpg" eager />);
    const media = view.container.querySelector("video")!;
    expect(media).not.toBe(previous);
    expect(media).not.toBeVisible();
    expect(previous).not.toHaveAttribute("src");
    expect(view.container.querySelector("img")).toBeVisible();
    fireEvent.seeked(previous);
    expect(media).not.toBeVisible();
    metadata(media);
    ready(media);
    expect(media).toBeVisible();
  });

  it("abandons stalled loading after twelve seconds and allows a transparent fallback", () => {
    vi.useFakeTimers();
    const view = render(<PlaybackFrame src="/first.mp4" positionSeconds={3_000} eager />);
    const media = view.container.querySelector("video")!;
    act(() => vi.advanceTimersByTime(12_000));
    expect(view.container.querySelector("video")).toBeNull();
    expect(view.container.querySelector("img")).toBeNull();
    expect(media).not.toHaveAttribute("src");
    expect(vi.getTimerCount()).toBe(0);
  });

  it("clears the timeout after success and releases media when unmounted", () => {
    vi.useFakeTimers();
    const view = render(<PlaybackFrame src="/first.mp4" positionSeconds={3_000} eager />);
    const media = view.container.querySelector("video")!;
    metadata(media);
    ready(media);
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(12_000));
    expect(media).toBeVisible();
    view.unmount();
    expect(media).not.toHaveAttribute("src");
    expect(vi.getTimerCount()).toBe(0);
  });
});
