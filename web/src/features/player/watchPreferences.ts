import { useEffect, useState } from "react";

const PAUSE_KEY = "seekino-watch-pause-on-send";
const FADE_KEY = "seekino-watch-fade-delay";
const CHANGE = "seekino:watch-preferences";
const DEFAULT_FADE = 8;

function readPreferences() {
  try {
    const seconds = Number(localStorage.getItem(FADE_KEY));
    return {
      pauseOnSend: localStorage.getItem(PAUSE_KEY) !== "false",
      fadeDelay: Number.isFinite(seconds) && seconds >= 1 ? seconds : DEFAULT_FADE,
    };
  } catch {
    return { pauseOnSend: true, fadeDelay: DEFAULT_FADE };
  }
}

type WatchPreferences = ReturnType<typeof readPreferences>;

function writePreference<K extends keyof WatchPreferences>(key: K, value: WatchPreferences[K]) {
  try { localStorage.setItem(key === "pauseOnSend" ? PAUSE_KEY : FADE_KEY, String(value)); }
  catch { /* Mounted views can still share preferences for this visit. */ }
  window.dispatchEvent(new CustomEvent<Partial<WatchPreferences>>(CHANGE, { detail: { [key]: value } }));
}

const setPauseOnSend = (value: boolean) => writePreference("pauseOnSend", value);
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
      if (event.key === null || event.key === PAUSE_KEY || event.key === FADE_KEY) setPreferences(readPreferences());
    };
    window.addEventListener(CHANGE, onLocal);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(CHANGE, onLocal);
      window.removeEventListener("storage", onStorage);
    };
  }, []);
  return { ...preferences, setPauseOnSend, setFadeDelay };
}
