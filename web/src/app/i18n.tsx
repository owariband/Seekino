import { createContext, type PropsWithChildren, useContext, useEffect, useMemo, useState } from "react";

import { localeTag, translate, type Locale, type MessageParams } from "../shared/messages";

export const LOCALE_STORAGE_KEY = "anivag-locale";

interface I18nValue {
  locale: Locale;
  formatLocale: string;
  setLocale: (locale: Locale) => void;
  t: (message: string, params?: MessageParams) => string;
}

const I18nContext = createContext<I18nValue>({
  locale: "zh", formatLocale: "zh-CN", setLocale: () => {},
  t: (message, params) => translate("zh", message, params),
});

function initialLocale(): Locale {
  try { return window.localStorage.getItem(LOCALE_STORAGE_KEY) === "en" ? "en" : "zh"; }
  catch { return "zh"; }
}

export function I18nProvider({ children }: PropsWithChildren) {
  const [locale, setLocale] = useState<Locale>(initialLocale);
  useEffect(() => {
    document.documentElement.lang = locale === "en" ? "en" : "zh-CN";
    document.title = translate(locale, "Seekino · 观看与发现");
    document.querySelector('meta[name="description"]')?.setAttribute(
      "content", translate(locale, "Seekino — 边看边问，用一句话找回视频片段。"),
    );
    try { window.localStorage.setItem(LOCALE_STORAGE_KEY, locale); } catch { /* The current tab can still switch languages. */ }
  }, [locale]);
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key === LOCALE_STORAGE_KEY) setLocale(event.newValue === "en" ? "en" : "zh");
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);
  const value = useMemo<I18nValue>(() => ({
    locale, setLocale, formatLocale: localeTag(locale),
    t: (message, params) => translate(locale, message, params),
  }), [locale]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  return useContext(I18nContext);
}
