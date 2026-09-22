export interface WatchProgress {
  seriesId: string;
  videoId: string;
  positionMs: number;
  durationMs: number;
  updatedAt: number;
}

const STORAGE_KEY = "anivag-watch-progress:v1";
const MAX_HISTORY = 100;

function isWatchProgress(value: unknown): value is WatchProgress {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<WatchProgress>;
  return typeof item.seriesId === "string" && item.seriesId.length > 0
    && typeof item.videoId === "string" && item.videoId.length > 0
    && typeof item.positionMs === "number" && Number.isSafeInteger(item.positionMs) && item.positionMs >= 0
    && typeof item.durationMs === "number" && Number.isSafeInteger(item.durationMs) && item.durationMs >= 0
    && typeof item.updatedAt === "number" && Number.isSafeInteger(item.updatedAt) && item.updatedAt >= 0;
}

export function readWatchHistory(): WatchProgress[] {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]");
    if (!Array.isArray(stored)) return [];
    const seen = new Set<string>();
    return stored.filter(isWatchProgress)
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .filter((item) => {
        if (seen.has(item.videoId)) return false;
        seen.add(item.videoId);
        return true;
      })
      .slice(0, MAX_HISTORY);
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
  updated.positionMs = Math.min(updated.positionMs, updated.durationMs);
  try {
    const history = readWatchHistory().filter((item) => item.videoId !== updated.videoId);
    localStorage.setItem(STORAGE_KEY, JSON.stringify([updated, ...history].slice(0, MAX_HISTORY)));
  } catch {
    // Browsers can disable or fill local storage; playback remains usable.
  }
}
