import { useCallback, useEffect, useRef, useState } from "react";

import { useI18n } from "../../app/i18n";
import { translate, type Locale } from "../../shared/messages";
import {
  ApiClientError,
  cancelAgentMessage,
  createAgentConversation,
  errorMessage,
  getAgentConversation,
  getAgentMessage,
  identityStorageKey,
  listAgentMessages,
  streamAgentMessage,
  subscribeAgentMessages,
  TENANT_ID,
} from "../../api/client";
import type {
  AgentAnswerEvent,
  AgentAttachmentReference,
  AgentConversationView,
  AgentMessageTiming,
  AgentMessageRunOptions,
  AgentMessageView,
  EvidenceReference,
} from "../../api/contracts";

interface StoredIdentity { key: string; id?: string }
const conversations = new Map<string, Promise<AgentConversationView>>();
const conversationStorageKey = () => identityStorageKey("anivag-agent-conversation");

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
  const storageKey = conversationStorageKey();
  const tenantId = TENANT_ID;
  const pending = conversations.get(storageKey);
  if (pending) return pending;
  const promise = (async () => {
    localStorage.removeItem(`anivag-agent-session:${TENANT_ID}`);
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
        if (conversation.tenantId === tenantId) return conversation;
      } catch (error) {
        if (!(error instanceof ApiClientError) || error.status !== 404) throw error;
      }
      if (TENANT_ID !== tenantId || conversationStorageKey() !== storageKey) throw new DOMException("Identity changed", "AbortError");
      identity = { key: crypto.randomUUID() };
      localStorage.setItem(storageKey, JSON.stringify(identity));
    }
    return createAgentConversation(identity.key);
  })();
  conversations.set(storageKey, promise);
  try { return await promise; }
  finally { conversations.delete(storageKey); }
}

function rememberConversation(conversationId: string): void {
  const storageKey = conversationStorageKey();
  const identity = storedIdentity(storageKey);
  localStorage.setItem(storageKey, JSON.stringify({
    key: !identity.id || identity.id === conversationId ? identity.key : crypto.randomUUID(),
    id: conversationId,
  }));
}

async function loadMessages(conversationId: string, locale: Locale): Promise<AgentMessageView[]> {
  const messages: AgentMessageView[] = [];
  let cursor: string | undefined;
  while (true) {
    const page = await listAgentMessages(conversationId, -1, cursor);
    messages.push(...page.items);
    if (!page.hasMore) return messages;
    if (!page.nextCursor) throw new Error(translate(locale, "消息列表缺少下一页游标"));
    cursor = page.nextCursor;
  }
}

function richerTiming(
  previous: AgentMessageTiming | null | undefined,
  next: AgentMessageTiming | null | undefined,
): AgentMessageTiming | null {
  if (!previous) return next ?? null;
  if (!next) return previous;
  if (next.totalDurationMs !== null) return next;
  if (previous.totalDurationMs !== null) return previous;
  return next.stages.length >= previous.stages.length ? next : previous;
}

function mergeMessages(
  current: AgentMessageView[], incoming: AgentMessageView[], conversationId: string,
  streamedMessageId: string | null = null,
  replaceProcessing = false,
): AgentMessageView[] {
  const messages = new Map(current.map((message) => [message.messageId, message]));
  for (const next of incoming) {
    if (next.conversationId !== conversationId) continue;
    const previous = messages.get(next.messageId);
    if (previous && next.version < previous.version) continue;
    if (previous && previous.status !== "PROCESSING") {
      if (next.status === "PROCESSING") continue;
    }
    messages.set(next.messageId, !replaceProcessing && previous?.status === "PROCESSING" && next.status === "PROCESSING"
      ? {
          ...next,
          version: next.messageId === streamedMessageId ? previous.version : next.version,
          phase: next.messageId === streamedMessageId ? previous.phase : next.phase,
          content: next.messageId === streamedMessageId ? previous.content : next.content ?? previous.content,
          searchResult: next.messageId === streamedMessageId
            ? previous.searchResult ?? next.searchResult
            : next.searchResult ?? previous.searchResult,
          timing: richerTiming(previous.timing, next.timing),
          renderBlocks: next.messageId === streamedMessageId
            ? previous.renderBlocks
            : next.renderBlocks ?? previous.renderBlocks,
        }
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
  cancellingMessageId: string | null;
  error: string | null;
}
export type AgentProgressEvent = Extract<AgentAnswerEvent, { type: "answer.progress" }>;
interface ActiveConversation {
  conversationId: string | null;
  controller: AbortController;
  unsubscribe?: () => void;
  sending: boolean;
  sendController: AbortController | null;
  cancellingMessageId: string | null;
  identityKey: string;
  recoveryMessageId: string | null;
  streamedMessageId: string | null;
}

export function useAgentConversation(requestedId: string | null = null) {
  const { locale } = useI18n();
  const localeRef = useRef(locale);
  localeRef.current = locale;
  const t = (message: string) => translate(localeRef.current, message);
  const [state, setState] = useState<ConversationState>({
    requestedId, conversationId: null, messages: [], progress: {}, loading: true, sending: false, cancellingMessageId: null, error: null,
  });
  const activeRef = useRef<ActiveConversation | null>(null);

  useEffect(() => {
    const active: ActiveConversation = { conversationId: null, controller: new AbortController(), sending: false, sendController: null, cancellingMessageId: null, identityKey: conversationStorageKey(), recoveryMessageId: null, streamedMessageId: null };
    activeRef.current = active;
    setState({ requestedId, conversationId: null, messages: [], progress: {}, loading: true, sending: false, cancellingMessageId: null, error: null });
    void (async () => {
      try {
        const conversation = requestedId ? await getAgentConversation(requestedId) : await ensureConversation();
        if (conversation.tenantId !== TENANT_ID) throw new Error(t("对话不属于当前工作区"));
        if (activeRef.current !== active) return;
        rememberConversation(conversation.conversationId);
        active.conversationId = conversation.conversationId;
        const messages = await loadMessages(conversation.conversationId, localeRef.current);
        if (activeRef.current !== active) return;
        setState((current) => ({ ...current, conversationId: conversation.conversationId, messages, loading: false }));
        active.unsubscribe = subscribeAgentMessages(conversation.conversationId, (snapshot) => {
          if (activeRef.current !== active) return;
          const recovered = snapshot.messages.some((message) => message.messageId === active.recoveryMessageId && message.status !== "PROCESSING");
          if (recovered) active.recoveryMessageId = null;
          setState((current) => ({ ...current, error: recovered ? null : current.error, messages: mergeMessages(current.messages, snapshot.messages, conversation.conversationId, active.sending ? active.streamedMessageId : null) }));
        });
      } catch (error) {
        if (activeRef.current === active) setState((current) => ({ ...current, loading: false, error: errorMessage(error, localeRef.current) }));
      }
    })();
    return () => {
      if (activeRef.current === active) activeRef.current = null;
      active.controller.abort();
      active.unsubscribe?.();
      active.unsubscribe = undefined;
    };
  }, [requestedId]);

  const clear = useCallback(() => {
    const active = activeRef.current;
    if (active) {
      activeRef.current = null;
      active.controller.abort();
      active.unsubscribe?.();
      active.unsubscribe = undefined;
    }
    setState((current) => ({
      requestedId: current.requestedId,
      conversationId: null,
      messages: [],
      progress: {},
      loading: false,
      sending: false,
      cancellingMessageId: null,
      error: null,
    }));
  }, []);

  useEffect(() => {
    const active = activeRef.current;
    if (!active || state.requestedId !== requestedId || active.identityKey !== conversationStorageKey() || !state.cancellingMessageId || !state.messages.some((message) => message.messageId === state.cancellingMessageId && message.status !== "PROCESSING")) return;
    const finishedSend = active.streamedMessageId === state.cancellingMessageId;
    if (finishedSend) {
      active.sendController?.abort();
      active.sendController = null;
      active.sending = false;
      active.streamedMessageId = null;
    }
    if (active?.cancellingMessageId === state.cancellingMessageId) active.cancellingMessageId = null;
    setState((current) => ({ ...current, sending: finishedSend ? false : current.sending, cancellingMessageId: null }));
  }, [requestedId, state.requestedId, state.cancellingMessageId, state.messages]);

  const stop = useCallback(async () => {
    const active = activeRef.current;
    const message = [...state.messages].reverse().find((item) => item.role === "ASSISTANT" && item.status === "PROCESSING");
    if (!active?.conversationId || !message || active.cancellingMessageId || state.requestedId !== requestedId || active.identityKey !== conversationStorageKey()) return;
    const conversationId = active.conversationId;
    active.cancellingMessageId = message.messageId;
    setState((current) => ({ ...current, cancellingMessageId: message.messageId, error: null }));
    try {
      const pair = await cancelAgentMessage(conversationId, message.messageId);
      if (activeRef.current !== active || active.identityKey !== conversationStorageKey() || active.cancellingMessageId !== message.messageId) return;
      const terminal = pair.assistantMessage.status !== "PROCESSING";
      if (terminal) {
        active.cancellingMessageId = null;
        active.sendController?.abort();
        active.sendController = null;
        active.sending = false;
        active.streamedMessageId = null;
      }
      setState((current) => ({
        ...current, messages: mergeMessages(current.messages, [pair.userMessage, pair.assistantMessage], conversationId, active.streamedMessageId),
        sending: terminal ? false : current.sending,
        cancellingMessageId: terminal ? null : message.messageId,
        error: terminal ? pair.assistantMessage.lastError?.message ?? null : current.error,
      }));
    } catch (error) {
      if (activeRef.current !== active || active.identityKey !== conversationStorageKey() || active.cancellingMessageId !== message.messageId) return;
      active.cancellingMessageId = null;
      setState((current) => ({ ...current, cancellingMessageId: null, error: errorMessage(error, localeRef.current) }));
    }
  }, [requestedId, state.messages, state.requestedId]);

  const send = useCallback(async (
    content: string,
    seriesIds: string[] = [],
    reference: EvidenceReference | null = null,
    options: AgentMessageRunOptions = {},
    extraContext: Record<string, unknown> | null = null,
    submission?: { attachments?: AgentAttachmentReference[]; onAccepted?: () => void },
  ) => {
    const active = activeRef.current;
    if (!content.trim() || !active?.conversationId || state.requestedId !== requestedId || active.sending || state.messages.some((message) => message.status === "PROCESSING")) return;
    const conversationId = active.conversationId;
    const scope = [...seriesIds];
    const frozenReference = reference ? { ...reference } : null;
    const frozenOptions = { ...options };
    const frozenExtraContext = extraContext === null ? null : structuredClone(extraContext);
    const frozenAttachments = (submission?.attachments ?? []).map(({ mediaUri }) => ({ mediaUri }));
    const onAccepted = submission?.onAccepted;
    active.sending = true;
    active.cancellingMessageId = null;
    const sendController = new AbortController();
    active.sendController = sendController;
    const signal = AbortSignal.any([active.controller.signal, sendController.signal]);
    active.recoveryMessageId = null;
    setState((current) => ({ ...current, sending: true, cancellingMessageId: null, error: null }));
    let messageId: string | null = null;
    const idempotencyKey = crypto.randomUUID();
    let sequence = -1;
    let accepted = false;
    let rejected = false;
    const accept = () => {
      if (accepted || signal.aborted || activeRef.current !== active || active.identityKey !== conversationStorageKey()) return;
      accepted = true;
      onAccepted?.();
    };
    const onEvent = (event: AgentAnswerEvent) => {
      if (activeRef.current !== active || active.sendController !== sendController || event.conversationId !== conversationId) return;
      if (!Number.isInteger(event.sequence) || event.sequence <= 0 || event.sequence <= sequence) return;
      if (event.type === "answer.started" || event.type === "answer.snapshot" || event.type === "answer.completed" || event.type === "answer.failed") {
        if (event.assistantMessage.messageId !== event.messageId || event.assistantMessage.conversationId !== conversationId || event.userMessage.conversationId !== conversationId) return;
        if (event.type === "answer.snapshot" && event.assistantMessage.status !== "PROCESSING") return;
      }
      if (event.type === "answer.started") {
        if (messageId && event.messageId !== messageId) return;
        messageId = event.messageId;
        active.streamedMessageId = messageId;
      } else if (event.messageId !== messageId) return;
      sequence = event.sequence;
      if (event.type === "answer.started") accept();
      if (event.type === "answer.interrupted") active.recoveryMessageId = event.messageId;
      setState((current) => {
        if (current.conversationId !== conversationId) return current;
        if (event.type === "answer.started" || event.type === "answer.snapshot" || event.type === "answer.completed") {
          const phase = event.assistantMessage.phase;
          const progress = event.type === "answer.started" && phase && !current.progress[event.messageId]
            ? { ...current.progress, [event.messageId]: [{ type: "answer.progress" as const, conversationId, messageId: event.messageId, sequence: event.sequence, occurredAt: event.occurredAt, phase, timing: event.assistantMessage.timing }] }
            : current.progress;
          return { ...current, progress, messages: mergeMessages(current.messages, [event.userMessage, event.assistantMessage], conversationId, null, event.type === "answer.snapshot") };
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
          if (event.type === "answer.delta") return { ...message, content: (message.content ?? "") + event.delta };
          if (event.type === "answer.block") return { ...message, renderBlocks: [...message.renderBlocks, event.block] };
          if (event.type === "answer.evidence") return { ...message, searchResult: event.searchResult };
          if (event.type === "answer.progress") return {
            ...message,
            phase: event.phase,
            timing: event.timing ?? message.timing,
          };
          return message;
        }) };
      });
    };
    try {
      await streamAgentMessage(
        conversationId,
        content.trim(),
        idempotencyKey,
        onEvent,
        signal,
        scope,
        frozenReference,
        frozenOptions,
        frozenExtraContext,
        frozenAttachments,
      );
    } catch (error) {
      rejected = error instanceof ApiClientError && error.status >= 400 && error.status < 500;
      if (activeRef.current === active && active.sendController === sendController && !signal.aborted) {
        active.recoveryMessageId = messageId;
        setState((current) => ({ ...current, error: errorMessage(error, localeRef.current) }));
      }
    } finally {
      if (active.sendController !== sendController) return;
      // A clean EOF can also lose the first event; only the request key proves acceptance.
      if (!accepted && !rejected && activeRef.current === active && !signal.aborted) {
        try {
          const pair = await getAgentMessage(conversationId, { idempotencyKey });
          if (activeRef.current === active && active.sendController === sendController && !signal.aborted && pair.userMessage.conversationId === conversationId && pair.assistantMessage.conversationId === conversationId) {
            accept();
            active.recoveryMessageId = pair.assistantMessage.status === "PROCESSING" ? pair.assistantMessage.messageId : null;
            setState((current) => ({ ...current, error: pair.assistantMessage.status === "PROCESSING" ? current.error : null, messages: mergeMessages(current.messages, [pair.userMessage, pair.assistantMessage], conversationId) }));
          }
        } catch {
          if (activeRef.current === active && !signal.aborted) setState((current) => ({ ...current, error: current.error ?? t("未能确认消息是否已接收，草稿已保留") }));
        }
      }
      if (active.sendController !== sendController) return;
      active.sendController = null;
      active.sending = false;
      active.streamedMessageId = null;
      if (activeRef.current === active) {
        setState((current) => ({ ...current, sending: false }));
        // The HTTP subscription can end while the service still owns the Kick.
        try {
          const messages = await loadMessages(conversationId, localeRef.current);
          if (activeRef.current === active) {
            const recovered = messages.some((message) => message.messageId === active.recoveryMessageId && message.status !== "PROCESSING");
            if (recovered) active.recoveryMessageId = null;
            setState((current) => ({ ...current, error: recovered ? null : current.error, messages: mergeMessages(current.messages, messages, conversationId) }));
          }
        } catch (error) {
          if (activeRef.current === active) setState((current) => ({ ...current, error: current.error ?? errorMessage(error, localeRef.current) }));
        }
      }
    }
  }, [requestedId, state.requestedId, state.messages]);

  const visible = state.requestedId === requestedId ? state : {
    requestedId, conversationId: null, messages: [], progress: {}, loading: true, sending: false, cancellingMessageId: null, error: null,
  };
  return {
    ...visible,
    searching: visible.sending || visible.messages.some((message) => message.status === "PROCESSING"),
    cancelling: Boolean(visible.cancellingMessageId),
    clear,
    send,
    stop,
  };
}
