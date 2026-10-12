import { useEffect, useSyncExternalStore } from "react";

// Counts mounted assistants that are answering, so the header mascot can
// keep "seeking" while the chat panel is closed or scrolled away.
let busyCount = 0;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function setBusy(delta: number) {
  busyCount = Math.max(0, busyCount + delta);
  listeners.forEach((listener) => listener());
}

export function useReportAssistantBusy(busy: boolean): void {
  useEffect(() => {
    if (!busy) return undefined;
    setBusy(1);
    return () => setBusy(-1);
  }, [busy]);
}

export function useAssistantBusy(): boolean {
  return useSyncExternalStore(subscribe, () => busyCount > 0, () => false);
}
