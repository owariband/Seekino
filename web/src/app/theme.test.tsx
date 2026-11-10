import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeProvider, useTheme } from "./theme";

const STORAGE_KEY = "seekino-theme";
const nativeMatchMedia = window.matchMedia;

function ThemeControl() {
  const { toggleTheme } = useTheme();
  return <button type="button" onClick={toggleTheme}>toggle</button>;
}

beforeEach(() => localStorage.clear());

afterEach(() => {
  cleanup();
  window.matchMedia = nativeMatchMedia;
  vi.restoreAllMocks();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  document.documentElement.style.removeProperty("color-scheme");
});

describe("theme preference", () => {
  it("applies a restored preference and persists manual changes", () => {
    localStorage.setItem(STORAGE_KEY, "dark");
    render(<ThemeProvider><ThemeControl /></ThemeProvider>);

    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");

    fireEvent.click(screen.getByRole("button", { name: "toggle" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(document.documentElement.style.colorScheme).toBe("light");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("light");

    fireEvent.click(screen.getByRole("button", { name: "toggle" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(localStorage.getItem(STORAGE_KEY)).toBe("dark");
  });

  it("falls back to the system preference when browser storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Blocked storage"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Blocked storage"); });
    window.matchMedia = (query) => ({ ...nativeMatchMedia(query), matches: query === "(prefers-color-scheme: dark)" });

    render(<ThemeProvider><ThemeControl /></ThemeProvider>);

    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });
});
