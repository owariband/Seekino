import { useEffect, useState } from "react";

const PAUSE_KEY = "seekino-watch-pause-on-send";
const FADE_KEY = "seekino-watch-fade-delay";
const VISUAL_KEY = "seekino-watch-visual-find";
const CHANGE = "seekino:watch-preferences";
const DEFAULT_FADE = 8;

function readPreferences() {
  try {
    const seconds = Number(localStorage.getItem(FADE_KEY));
    return {
      pauseOnSend: localStorage.getItem(PAUSE_KEY) !== "false",
      visualFind: localStorage.getItem(VISUAL_KEY) !== "false",
      fadeDelay: Number.isFinite(seconds) && seconds >= 1 ? seconds : DEFAULT_FADE,
    };
  } catch {
    return { pauseOnSend: true, fadeDelay: DEFAULT_FADE, visualFind: true };
  }
}

type WatchPreferences = ReturnType<typeof readPreferences>;

const keys = { pauseOnSend: PAUSE_KEY, fadeDelay: FADE_KEY, visualFind: VISUAL_KEY };

function writePreference<K extends keyof WatchPreferences>(key: K, value: WatchPreferences[K]) {
  try { localStorage.setItem(keys[key], String(value)); }
  catch { /* Mounted views can still share preferences for this visit. */ }
  window.dispatchEvent(new CustomEvent<Partial<WatchPreferences>>(CHANGE, { detail: { [key]: value } }));
}

const setPauseOnSend = (value: boolean) => writePreference("pauseOnSend", value);
const setVisualFind = (value: boolean) => writePreference("visualFind", value);
const setFadeDelay = (seconds: number) => {
  if (Number.isFinite(seconds) && seconds >= 1) writePreference("fadeDelay", seconds);
};

export function useWatchPreferences() {
  const [preferences, setPreferences] = useState(readPreferences);
  useEffect(() => {
    const onLocal = (event: Event) => setPreferences(current => ({
      ...current, ...(event as CustomEvent<Partial<WatchPreferences>>).detail,
    }));
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || Object.values(keys).includes(event.key)) setPreferences(readPreferences());
    };
    window.addEventListener(CHANGE, onLocal);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(CHANGE, onLocal);
      window.removeEventListener("storage", onStorage);
    };
  }, []);
  return { ...preferences, setPauseOnSend, setFadeDelay, setVisualFind };
}
