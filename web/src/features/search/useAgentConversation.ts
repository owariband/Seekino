import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiClientError,
  createAgentConversation,
  errorMessage,
  getAgentConversation,
  getAgentMessage,
  listAgentMessages,
  streamAgentMessage,
  subscribeAgentMessages,
  WORKSPACE_ID,
} from "../../api/client";
import type {
  AgentAnswerEvent,
  AgentConversationView,
  AgentMessageView,
  EvidenceReference,
} from "../../api/contracts";

interface StoredIdentity { key: string; id?: string }
const conversations = new Map<string, Promise<AgentConversationView>>();

function storedIdentity(storageKey: string): StoredIdentity {
  const saved = localStorage.getItem(storageKey);
  if (saved) {
    try {
      const value = JSON.parse(saved) as StoredIdentity;
      if (typeof value.key === "string" && (!value.id || typeof value.id === "string")) return value;
    } catch { /* Replace an unreadable browser record. */ }
  }
  const identity = { key: crypto.randomUUID() };
  localStorage.setItem(storageKey, JSON.stringify(identity));
  return identity;
}

async function ensureConversation(): Promise<AgentConversationView> {
  const storageKey = `anivag-agent-conversation:${WORKSPACE_ID}`;
  const pending = conversations.get(storageKey);
  if (pending) return pending;
  const promise = (async () => {
    localStorage.removeItem(`anivag-agent-session:${WORKSPACE_ID}`);
    const legacySeriesId = localStorage.getItem("anivag-selected-series");
    const legacyIdentity = legacySeriesId ? localStorage.getItem(`${storageKey}:${legacySeriesId}`) : null;
    if (!localStorage.getItem(storageKey) && legacyIdentity) {
      try {
        const legacy = JSON.parse(legacyIdentity) as StoredIdentity;
        if (typeof legacy.id === "string" && typeof legacy.key === "string") localStorage.setItem(storageKey, legacyIdentity);
      } catch { /* An unfinished legacy creation needs a new idempotency key. */ }
    }
    let identity = storedIdentity(storageKey);
    if (identity.id) {
      try {
        const conversation = await getAgentConversation(identity.id);
        if (conversation.workspaceId === WORKSPACE_ID) return conversation;
      } catch (error) {
        if (!(error instanceof ApiClientError) || error.status !== 404) throw error;
      }
      identity = { key: crypto.randomUUID() };
      localStorage.setItem(storageKey, JSON.stringify(identity));
    }
    const conversation = await createAgentConversation(identity.key);
    localStorage.setItem(storageKey, JSON.stringify({ ...identity, id: conversation.conversationId }));
    return conversation;
  })();
  conversations.set(storageKey, promise);
  try { return await promise; }
  finally { conversations.delete(storageKey); }
}

async function loadMessages(conversationId: string): Promise<AgentMessageView[]> {
  const messages: AgentMessageView[] = [];
  let cursor: string | undefined;
  while (true) {
    const page = await listAgentMessages(conversationId, -1, cursor);
    messages.push(...page.items);
    if (!page.hasMore) return messages;
    if (!page.nextCursor) throw new Error("消息列表缺少下一页游标");
    cursor = page.nextCursor;
  }
}

function mergeMessages(
  current: AgentMessageView[], incoming: AgentMessageView[], conversationId: string,
  streamedMessageId: string | null = null,
): AgentMessageView[] {
  const messages = new Map(current.map((message) => [message.messageId, message]));
  for (const next of incoming) {
    if (next.conversationId !== conversationId) continue;
    const previous = messages.get(next.messageId);
    if (previous && next.version < previous.version) continue;
    if (previous && previous.status !== "PROCESSING") {
      if (next.status === "PROCESSING") continue;
    }
    messages.set(next.messageId, previous?.status === "PROCESSING" && next.status === "PROCESSING"
      ? { ...next, phase: next.messageId === streamedMessageId ? previous.phase : next.phase, content: next.messageId === streamedMessageId ? previous.content : next.content ?? previous.content, searchResult: previous.searchResult ?? next.searchResult }
      : next);
  }
  return [...messages.values()].sort((left, right) => left.sequenceNo - right.sequenceNo);
}

interface ConversationState {
  requestedId: string | null;
  conversationId: string | null;
  messages: AgentMessageView[];
  progress: Record<string, AgentProgressEvent[]>;
  loading: boolean;
  sending: boolean;
  error: string | null;
}
export type AgentProgressEvent = Extract<AgentAnswerEvent, { type: "answer.progress" }>;
interface ActiveConversation {
  conversationId: string | null;
  controller: AbortController;
  sending: boolean;
  recoveryMessageId: string | null;
  streamedMessageId: string | null;
}

export function useAgentConversation(requestedId: string | null = null) {
  const [state, setState] = useState<ConversationState>({
    requestedId, conversationId: null, messages: [], progress: {}, loading: true, sending: false, error: null,
  });
  const activeRef = useRef<ActiveConversation | null>(null);

  useEffect(() => {
    const active: ActiveConversation = { conversationId: null, controller: new AbortController(), sending: false, recoveryMessageId: null, streamedMessageId: null };
    activeRef.current = active;
    let unsubscribe: (() => void) | undefined;
    setState({ requestedId, conversationId: null, messages: [], progress: {}, loading: true, sending: false, error: null });
    void (async () => {
      try {
        const conversation = requestedId ? await getAgentConversation(requestedId) : await ensureConversation();
        if (conversation.workspaceId !== WORKSPACE_ID) throw new Error("对话不属于当前工作区");
        if (activeRef.current !== active) return;
        active.conversationId = conversation.conversationId;
        const messages = await loadMessages(conversation.conversationId);
        if (activeRef.current !== active) return;
        setState((current) => ({ ...current, conversationId: conversation.conversationId, messages, loading: false }));
        unsubscribe = subscribeAgentMessages(conversation.conversationId, (snapshot) => {
          if (activeRef.current !== active) return;
          const recovered = snapshot.messages.some((message) => message.messageId === active.recoveryMessageId && message.status !== "PROCESSING");
          if (recovered) active.recoveryMessageId = null;
          setState((current) => ({ ...current, error: recovered ? null : current.error, messages: mergeMessages(current.messages, snapshot.messages, conversation.conversationId, active.sending ? active.streamedMessageId : null) }));
        });
      } catch (error) {
        if (activeRef.current === active) setState((current) => ({ ...current, loading: false, error: errorMessage(error) }));
      }
    })();
    return () => {
      if (activeRef.current === active) activeRef.current = null;
      active.controller.abort();
      unsubscribe?.();
    };
  }, [requestedId]);

  const send = useCallback(async (content: string, seriesIds: string[] = [], reference: EvidenceReference | null = null) => {
    const active = activeRef.current;
    if (!content.trim() || !active?.conversationId || state.requestedId !== requestedId || active.sending || state.messages.some((message) => message.status === "PROCESSING")) return;
    const conversationId = active.conversationId;
    const scope = [...seriesIds];
    const frozenReference = reference ? { ...reference } : null;
    active.sending = true;
    active.recoveryMessageId = null;
    setState((current) => ({ ...current, sending: true, error: null }));
    let messageId: string | null = null;
    let deltaBuffer = "";
    const idempotencyKey = crypto.randomUUID();
    let sequence = -1;
    const onEvent = (event: AgentAnswerEvent) => {
      if (activeRef.current !== active || event.conversationId !== conversationId) return;
      if (!Number.isInteger(event.sequence) || event.sequence <= 0 || event.sequence <= sequence) return;
      if (event.type === "answer.started") {
        if (messageId && event.messageId !== messageId) return;
        if (event.assistantMessage.messageId !== event.messageId) return;
        messageId = event.messageId;
        active.streamedMessageId = messageId;
      } else if (event.messageId !== messageId) return;
      sequence = event.sequence;
      if (event.type === "answer.delta") deltaBuffer += event.delta;
      if (event.type === "answer.interrupted") active.recoveryMessageId = event.messageId;
      setState((current) => {
        if (current.conversationId !== conversationId) return current;
        if (event.type === "answer.started" || event.type === "answer.completed") {
          const phase = event.assistantMessage.phase;
          const progress = event.type === "answer.started" && phase && !current.progress[event.messageId]
            ? { ...current.progress, [event.messageId]: [{ type: "answer.progress" as const, conversationId, messageId: event.messageId, sequence: event.sequence, occurredAt: event.occurredAt, phase }] }
            : current.progress;
          return { ...current, progress, messages: mergeMessages(current.messages, [event.userMessage, event.assistantMessage], conversationId) };
        }
        if (event.type === "answer.failed") {
          const messages = mergeMessages(current.messages, [event.userMessage, event.assistantMessage], conversationId);
          return { ...current, messages, error: event.error.message };
        }
        if (event.type === "answer.interrupted") return { ...current, error: event.error.message };
        if (!current.messages.some((message) => message.messageId === event.messageId && message.status === "PROCESSING")) return current;
        const steps = current.progress[event.messageId] ?? [];
        const progress = event.type === "answer.progress" && steps.at(-1)?.phase !== event.phase
          ? { ...current.progress, [event.messageId]: [...steps, event] }
          : current.progress;
        return { ...current, progress, messages: current.messages.map((message) => {
          if (message.messageId !== event.messageId || message.status !== "PROCESSING") return message;
          if (event.type === "answer.delta") return { ...message, content: deltaBuffer };
          if (event.type === "answer.evidence") return { ...message, searchResult: event.searchResult };
          if (event.type === "answer.progress") return { ...message, phase: event.phase };
          return message;
        }) };
      });
    };
    try {
      await streamAgentMessage(conversationId, content.trim(), idempotencyKey, onEvent, active.controller.signal, scope, 10, frozenReference);
    } catch (error) {
      if (activeRef.current === active && !active.controller.signal.aborted) {
        active.recoveryMessageId = messageId;
        setState((current) => ({ ...current, error: errorMessage(error) }));
        if (!messageId) {
          try {
            const pair = await getAgentMessage(conversationId, { idempotencyKey });
            if (activeRef.current === active) {
              active.recoveryMessageId = pair.assistantMessage.status === "PROCESSING" ? pair.assistantMessage.messageId : null;
              setState((current) => ({ ...current, error: pair.assistantMessage.status === "PROCESSING" ? current.error : null, messages: mergeMessages(current.messages, [pair.userMessage, pair.assistantMessage], conversationId) }));
            }
          } catch { /* A missing or unavailable lookup is reconciled by the snapshot below. */ }
        }
      }
    } finally {
      active.sending = false;
      active.streamedMessageId = null;
      if (activeRef.current === active) {
        setState((current) => ({ ...current, sending: false }));
        // The HTTP subscription can end while the service still owns the Kick.
        try {
          const messages = await loadMessages(conversationId);
          if (activeRef.current === active) {
            const recovered = messages.some((message) => message.messageId === active.recoveryMessageId && message.status !== "PROCESSING");
            if (recovered) active.recoveryMessageId = null;
            setState((current) => ({ ...current, error: recovered ? null : current.error, messages: mergeMessages(current.messages, messages, conversationId) }));
          }
        } catch (error) {
          if (activeRef.current === active) setState((current) => ({ ...current, error: errorMessage(error) }));
        }
      }
    }
  }, [requestedId, state.requestedId, state.messages]);

  const visible = state.requestedId === requestedId ? state : {
    requestedId, conversationId: null, messages: [], progress: {}, loading: true, sending: false, error: null,
  };
  return {
    ...visible,
    searching: visible.sending || visible.messages.some((message) => message.status === "PROCESSING"),
    send,
  };
}
