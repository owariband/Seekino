import { useCallback, useEffect, useRef, useState } from "react";

import { errorMessage, identityStorageKey, uploadAgentAttachment } from "../../api/client";
import type { AgentAttachmentUploadView } from "../../api/contracts";

// A file-picker hint; the server validates the actual format and static-image limits.
export const AGENT_ATTACHMENT_ACCEPT = "image/jpeg,image/png,image/webp";

export interface AgentAttachmentDraft {
  id: string;
  file: File;
  previewUrl: string;
  status: "uploading" | "ready" | "failed";
  error: string | null;
  attachment?: AgentAttachmentUploadView;
}

interface AttachmentEntry {
  draft: AgentAttachmentDraft;
  controller: AbortController | null;
}

interface AttachmentScope {
  conversationId: string;
  identityKey: string;
  entries: Map<string, AttachmentEntry>;
}

const attachmentIdentityKey = () => identityStorageKey("anivag-agent-attachments");

function releaseEntry(scope: AttachmentScope, id: string): void {
  const entry = scope.entries.get(id);
  if (!entry) return;
  scope.entries.delete(id);
  entry.controller?.abort();
  URL.revokeObjectURL(entry.draft.previewUrl);
}

export function useAgentAttachments(conversationId: string | null) {
  const identityKey = attachmentIdentityKey();
  const active = useRef<AttachmentScope | null>(null);
  const [snapshot, setSnapshot] = useState<{ scope: AttachmentScope | null; items: AgentAttachmentDraft[] }>({ scope: null, items: [] });

  useEffect(() => {
    const scope: AttachmentScope | null = conversationId ? { conversationId, identityKey, entries: new Map() } : null;
    active.current = scope;
    setSnapshot({ scope, items: [] });
    return () => {
      if (active.current === scope) active.current = null;
      if (scope) for (const id of scope.entries.keys()) releaseEntry(scope, id);
    };
  }, [conversationId, identityKey]);

  const publish = useCallback((scope: AttachmentScope) => {
    if (active.current !== scope || attachmentIdentityKey() !== scope.identityKey) return;
    setSnapshot({ scope, items: [...scope.entries.values()].map((entry) => entry.draft) });
  }, []);

  const upload = useCallback(async (scope: AttachmentScope, entry: AttachmentEntry) => {
    const controller = new AbortController();
    entry.controller = controller;
    entry.draft = { ...entry.draft, status: "uploading", error: null };
    publish(scope);
    const stillActive = () => active.current === scope
      && attachmentIdentityKey() === scope.identityKey
      && scope.entries.get(entry.draft.id) === entry
      && entry.controller === controller
      && !controller.signal.aborted;
    try {
      const attachment = await uploadAgentAttachment(scope.conversationId, entry.draft.file, controller.signal);
      if (!stillActive()) return;
      entry.draft = { ...entry.draft, status: "ready", attachment };
      publish(scope);
    } catch (error) {
      if (!stillActive()) return;
      entry.draft = { ...entry.draft, status: "failed", error: errorMessage(error) };
      publish(scope);
    } finally {
      if (entry.controller === controller) entry.controller = null;
    }
  }, [publish]);

  const currentScope = useCallback(() => {
    const scope = active.current;
    return scope?.conversationId === conversationId && scope.identityKey === identityKey
      && attachmentIdentityKey() === identityKey ? scope : null;
  }, [conversationId, identityKey]);

  const addFiles = useCallback((files: File[]) => {
    const scope = currentScope();
    if (!scope) return;
    const entries = files.map((file): AttachmentEntry => ({
      draft: { id: crypto.randomUUID(), file, previewUrl: URL.createObjectURL(file), status: "uploading", error: null },
      controller: null,
    }));
    for (const entry of entries) scope.entries.set(entry.draft.id, entry);
    for (const entry of entries) void upload(scope, entry);
  }, [currentScope, upload]);

  const clear = useCallback((ids?: readonly string[]) => {
    const scope = currentScope();
    if (!scope) return;
    for (const id of ids ?? [...scope.entries.keys()]) releaseEntry(scope, id);
    publish(scope);
  }, [currentScope, publish]);

  const remove = useCallback((id: string) => clear([id]), [clear]);
  const retry = useCallback((id: string) => {
    const scope = currentScope();
    const entry = scope?.entries.get(id);
    if (scope && entry?.draft.status === "failed") void upload(scope, entry);
  }, [currentScope, upload]);

  const items = snapshot.scope?.conversationId === conversationId && snapshot.scope.identityKey === identityKey ? snapshot.items : [];
  return { items, addFiles, remove, retry, clear };
}
