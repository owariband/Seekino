export interface WatchProgress {
  seriesId: string;
  videoId: string;
  /** Integer milliseconds; historical field names are retained. */
  positionSeconds: number;
  durationSeconds: number;
  updatedAt: number;
}

interface LegacyWatchProgress {
  seriesId: string;
  videoId: string;
  positionMs: number;
  durationMs: number;
  updatedAt: number;
}

const STORAGE_KEY = "anivag-watch-progress:v3";
const SECONDS_STORAGE_KEY = "anivag-watch-progress:v2";
const LEGACY_STORAGE_KEY = "anivag-watch-progress:v1";
const MAX_HISTORY = 100;

function isWatchProgress(value: unknown): value is WatchProgress {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<WatchProgress>;
  return typeof item.seriesId === "string" && item.seriesId.length > 0
    && typeof item.videoId === "string" && item.videoId.length > 0
    && typeof item.positionSeconds === "number" && Number.isSafeInteger(item.positionSeconds) && item.positionSeconds >= 0
    && typeof item.durationSeconds === "number" && Number.isSafeInteger(item.durationSeconds) && item.durationSeconds >= 0
    && typeof item.updatedAt === "number" && Number.isSafeInteger(item.updatedAt) && item.updatedAt >= 0;
}

function isLegacyWatchProgress(value: unknown): value is LegacyWatchProgress {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<LegacyWatchProgress>;
  return typeof item.seriesId === "string" && item.seriesId.length > 0
    && typeof item.videoId === "string" && item.videoId.length > 0
    && typeof item.positionMs === "number" && Number.isSafeInteger(item.positionMs) && item.positionMs >= 0
    && typeof item.durationMs === "number" && Number.isSafeInteger(item.durationMs) && item.durationMs >= 0
    && typeof item.updatedAt === "number" && Number.isSafeInteger(item.updatedAt) && item.updatedAt >= 0;
}

function normalizeHistory(items: unknown[]): WatchProgress[] {
  const seen = new Set<string>();
  return items.filter(isWatchProgress)
    .map((item) => ({ ...item, positionSeconds: Math.min(item.positionSeconds, item.durationSeconds) }))
    .sort((left, right) => right.updatedAt - left.updatedAt)
    .filter((item) => {
      if (seen.has(item.videoId)) return false;
      seen.add(item.videoId);
      return true;
    })
    .slice(0, MAX_HISTORY);
}

function migrateLegacyHistory(): WatchProgress[] {
  const secondsEncoded = localStorage.getItem(SECONDS_STORAGE_KEY);
  const stored: unknown = JSON.parse(secondsEncoded ?? localStorage.getItem(LEGACY_STORAGE_KEY) ?? "[]");
  if (!Array.isArray(stored)) return [];
  const migrated = normalizeHistory(secondsEncoded !== null
    ? stored.filter(isWatchProgress).map((item) => ({
      ...item,
      positionSeconds: item.positionSeconds * 1000,
      durationSeconds: item.durationSeconds * 1000,
    }))
    : stored.filter(isLegacyWatchProgress).map((item) => ({
      seriesId: item.seriesId,
      videoId: item.videoId,
      positionSeconds: item.positionMs,
      durationSeconds: item.durationMs,
      updatedAt: item.updatedAt,
    })));
  localStorage.setItem(STORAGE_KEY, JSON.stringify(migrated));
  localStorage.removeItem(SECONDS_STORAGE_KEY);
  localStorage.removeItem(LEGACY_STORAGE_KEY);
  return migrated;
}

export function readWatchHistory(): WatchProgress[] {
  try {
    const encoded = localStorage.getItem(STORAGE_KEY);
    if (encoded === null) return migrateLegacyHistory();
    const stored: unknown = JSON.parse(encoded);
    if (!Array.isArray(stored)) return [];
    return normalizeHistory(stored);
  } catch {
    return [];
  }
}

export function readVideoProgress(videoId: string): WatchProgress | null {
  return readWatchHistory().find((item) => item.videoId === videoId) ?? null;
}

export function saveVideoProgress(progress: Omit<WatchProgress, "updatedAt">): void {
  const updated = { ...progress, updatedAt: Date.now() };
  if (!isWatchProgress(updated)) return;
  updated.positionSeconds = Math.min(updated.positionSeconds, updated.durationSeconds);
  try {
    const history = readWatchHistory().filter((item) => item.videoId !== updated.videoId);
    localStorage.setItem(STORAGE_KEY, JSON.stringify([updated, ...history].slice(0, MAX_HISTORY)));
  } catch {
    // Browsers can disable or fill local storage; playback remains usable.
  }
}
