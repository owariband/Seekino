import { act, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Kino } from "./Kino";

const svg = () => document.querySelector("svg[data-kino]")!;
const state = () => svg().getAttribute("data-kino-state");
let reduced = false;

beforeEach(() => {
  reduced = false;
  vi.useFakeTimers();
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string) => ({
      matches: reduced && query.includes("reduce"), media: query,
      addEventListener: () => undefined, removeEventListener: () => undefined,
    }),
  });
});
afterEach(() => vi.useRealTimers());

describe("Kino", () => {
  it("animates and settles into each state's pose", () => {
    const view = render(<Kino state="wander" />);
    const before = svg().innerHTML;
    act(() => { vi.advanceTimersByTime(300); });
    expect(svg().innerHTML).not.toBe(before);

    view.rerender(<Kino state="think" />);
    act(() => { vi.advanceTimersByTime(2000); });
    expect(svg().getAttribute("data-pose")).toBe("front");

    view.rerender(<Kino state="sleep" />);
    act(() => { vi.advanceTimersByTime(3000); });
    expect(svg().getAttribute("data-pose")).toMatch(/^sleep/);
  });

  it("plays a reaction once, reports it, and returns to its state", () => {
    const onEnd = vi.fn();
    const view = render(<Kino state="idle" reaction={{ name: "jump", key: 1 }} onReactionEnd={onEnd} />);
    expect(state()).toBe("jump");
    act(() => { vi.advanceTimersByTime(3000); });
    expect(onEnd).toHaveBeenCalledExactlyOnceWith("jump");
    expect(state()).toBe("idle");
    view.rerender(<Kino state="idle" reaction={{ name: "happy", key: 2 }} onReactionEnd={onEnd} />);
    expect(state()).toBe("happy");
  });

  it("looks when hovered, reacts when poked, and startles on the fifth poke", () => {
    render(<Kino interactive />);
    const button = document.querySelector("button")!;
    expect(button).toHaveAccessibleName("Kino");
    fireEvent.pointerEnter(button);
    expect(state()).toBe("look");
    fireEvent.click(button);
    expect(state()).toBe("jump");
    for (let i = 0; i < 4; i++) fireEvent.click(button);
    expect(state()).toBe("oops");
  });

  it("dozes off without activity and wakes on input", () => {
    render(<Kino state="idle" dozeAfterMs={10_000} />);
    act(() => { vi.advanceTimersByTime(9_000); });
    expect(state()).toBe("idle");
    act(() => { vi.advanceTimersByTime(2_000); });
    expect(state()).toBe("sleep");
    act(() => { fireEvent.keyDown(window, { key: "a" }); });
    expect(state()).toBe("idle");
  });

  it("holds one still frame and skips reactions when motion is reduced", () => {
    reduced = true;
    const onEnd = vi.fn();
    render(<Kino state="run" reaction={{ name: "happy", key: 1 }} onReactionEnd={onEnd} />);
    const still = svg().innerHTML;
    act(() => { vi.advanceTimersByTime(2000); });
    expect(svg().innerHTML).toBe(still);
    expect(svg().getAttribute("data-pose")).toBe("run");
    expect(onEnd).toHaveBeenCalledWith("happy");
  });

  it("is decorative by default and labelled when given a title", () => {
    const view = render(<Kino />);
    expect(svg().getAttribute("aria-hidden")).toBe("true");
    view.rerender(<Kino title="Kino is searching" state="loading" />);
    expect(svg().getAttribute("role")).toBe("img");
    expect(svg().getAttribute("aria-label")).toBe("Kino is searching");
  });

  it("sizes the SVG from the scene width and scale", () => {
    render(<Kino width={80} scale={2} />);
    expect(svg().getAttribute("viewBox")).toBe("0 0 80 28");
    expect(svg().getAttribute("width")).toBe("160");
    expect(svg().getAttribute("height")).toBe("56");
  });
});
