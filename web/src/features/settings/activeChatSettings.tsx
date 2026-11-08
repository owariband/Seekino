import { createContext, type PropsWithChildren, useContext, useLayoutEffect, useState } from "react";

interface ActiveChatSettings {
  modelId: string;
  modelSaving: boolean;
  modelError: string | null;
  loading: boolean;
  selectModel: (id: string) => Promise<void>;
}

const ActiveChatContext = createContext<ActiveChatSettings | null>(null);
const PublishChatContext = createContext<((chat: ActiveChatSettings | null) => void) | null>(null);

export function ActiveChatSettingsProvider({ children }: PropsWithChildren) {
  const [chat, publish] = useState<ActiveChatSettings | null>(null);
  return <PublishChatContext.Provider value={publish}>
    <ActiveChatContext.Provider value={chat}>{children}</ActiveChatContext.Provider>
  </PublishChatContext.Provider>;
}

export const useActiveChatSettings = () => useContext(ActiveChatContext);

/** The mounted chat owns the draft and all writes, including when settings cover it. */
export function usePublishChatSettings(enabled: boolean, { modelId, modelSaving, modelError, loading, selectModel }: ActiveChatSettings) {
  const publish = useContext(PublishChatContext);
  useLayoutEffect(() => {
    if (!enabled || !publish) return;
    publish({ modelId, modelSaving, modelError, loading, selectModel });
    return () => publish(null);
  }, [enabled, publish, modelId, modelSaving, modelError, loading, selectModel]);
}
