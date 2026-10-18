import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { englishMessages, translate } from "../shared/messages";
import { I18nProvider, LOCALE_STORAGE_KEY, useI18n } from "./i18n";

function LanguageControl() {
  const { locale, t, setLocale } = useI18n();
  return <>
    <p>{t("设置")}</p>
    <output>{locale}</output>
    <button onClick={() => setLocale("en")}>EN</button>
    <button onClick={() => setLocale("zh")}>ZH</button>
  </>;
}

beforeEach(() => localStorage.clear());
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("interface language", () => {
  it("defaults to Chinese and persists an immediate English switch across remounts", () => {
    const first = render(<I18nProvider><LanguageControl /></I18nProvider>);
    expect(screen.getByText("设置")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    expect(screen.getByText("Settings")).toBeInTheDocument();
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("en");
    expect(document.documentElement.lang).toBe("en");
    expect(document.title).toBe("Seekino · Watch and Discover");
    first.unmount();
    render(<I18nProvider><LanguageControl /></I18nProvider>);
    expect(screen.getByText("Settings")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "ZH" }));
    expect(document.documentElement.lang).toBe("zh-CN");
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe("zh");
  });

  it("syncs language changes from another tab without consuming unrelated storage", () => {
    localStorage.setItem("anivag-theme", "dark");
    render(<I18nProvider><LanguageControl /></I18nProvider>);
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: LOCALE_STORAGE_KEY, newValue: "en" })));
    expect(screen.getByText("Settings")).toBeInTheDocument();
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: "anivag-theme", newValue: "light" })));
    expect(screen.getByText("Settings")).toBeInTheDocument();
    expect(localStorage.getItem("anivag-theme")).toBe("dark");
  });

  it("keeps switching in the current tab when browser storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Blocked storage"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Blocked storage"); });
    render(<I18nProvider><LanguageControl /></I18nProvider>);
    fireEvent.click(screen.getByRole("button", { name: "EN" }));
    expect(screen.getByText("Settings")).toBeInTheDocument();
  });

  it("keeps interpolation placeholders intact and leaves business text unchanged", () => {
    for (const [source, english] of Object.entries(englishMessages)) {
      const placeholders = (value: string) => [...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
      expect(placeholders(english), source).toEqual(placeholders(source));
    }
    expect(translate("en", "第 {order} 集", { order: 13 })).toBe("Episode 13");
    expect(translate("en", "我的系列名 · 原始字幕")).toBe("我的系列名 · 原始字幕");
  });
});
