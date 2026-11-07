import { useEffect, useState } from "react";
import { getPersonalModelSettings, identityStorageKey } from "../../api/client";
import type { PersonalModelConnection, PersonalModelSettings } from "../../api/contracts";

const recentKey = () => identityStorageKey("seekino-recent-chat-model");

export function rememberChatModel(id: string) {
  if (!id.startsWith("personal:") && !id.startsWith("yaml:")) return;
  try { localStorage.setItem(recentKey(), id); } catch { /* Storage is optional. */ }
}

export function containsModel(connections: readonly PersonalModelConnection[], id: string) {
  return connections.some((connection) => connection.models.some((model) => model.id === id && model.available !== false));
}

export function savedChatModelId(connections: readonly PersonalModelConnection[], id: string) {
  for (const connection of connections) {
    const match = connection.models.find((model) => model.available !== false && model.aliases?.includes(id));
    if (match) return match.id;
  }
  return id;
}

export function preferredChatModel(settings: PersonalModelSettings) {
  if (containsModel(settings.connections, settings.defaultModelId)) return settings.defaultModelId;
  let recent: string | null = null;
  try { recent = localStorage.getItem(recentKey()); } catch { /* Storage is optional. */ }
  if (recent && containsModel(settings.connections, recent)) return recent;
  for (const connection of settings.connections) {
    const primary = connection.models.find((model) => model.isYamlDefault && model.available !== false);
    if (primary) return primary.id;
  }
  return "default";
}

type Catalog = { scope: string; settings: PersonalModelSettings } | { scope: string; error: unknown };

export function usePersonalChatModels(scope: string, enabled: boolean) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  useEffect(() => {
    if (!enabled) { setCatalog(null); return; }
    let pending: AbortController;
    const refresh = () => {
      pending?.abort();
      pending = new AbortController();
      const { signal } = pending;
      void getPersonalModelSettings(signal).then((settings) => {
        if (!Array.isArray(settings?.connections)) throw new Error("无法读取已配置的模型服务");
        if (!signal.aborted) setCatalog({ scope, settings });
      }).catch((error) => {
        if (!signal.aborted) setCatalog({ scope, error });
      });
    };
    refresh();
    window.addEventListener("focus", refresh);
    window.addEventListener("seekino:model-connections-changed", refresh);
    return () => {
      pending.abort();
      window.removeEventListener("focus", refresh);
      window.removeEventListener("seekino:model-connections-changed", refresh);
    };
  }, [scope, enabled]);
  return enabled && catalog?.scope === scope ? catalog : null;
}
