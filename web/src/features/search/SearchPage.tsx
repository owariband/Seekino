import { ArrowUp, ChevronDown, History, LoaderCircle, Play, Plus, SlidersHorizontal, Square, Trash2, TriangleAlert, X } from "lucide-react";
import { Children, createContext, forwardRef, type FormEvent, type ReactNode, useCallback, useEffect, useId, useContext, useImperativeHandle, useLayoutEffect, useRef, useState } from "react";
import Markdown, { type Components, defaultUrlTransform } from "react-markdown";
import { Link, useInRouterContext } from "react-router";
import remarkGfm from "remark-gfm";

import { useWorkspace } from "../../app/workspace";
import { createAgentConversation, deleteAgentConversation, errorMessage, getAgentMessageOptions, identityStorageKey, listAgentConversations, mediaUrl } from "../../api/client";
import type { AgentConversationView, AgentMessageOptions, AgentMessageRunOptions, AgentMessageView, AgentRenderBlock, DisplayMode, EvidenceReference } from "../../api/contracts";
import { episodeLabel, formatTimecode, formatTimeRange, formatTimeText, MEDIA_TIMESTAMP_PATTERN, timestampMilliseconds } from "../../shared/format";
import { useReportAssistantBusy } from "../../shared/assistantActivity";
import { Mascot } from "../../shared/Mascot";
import { useAuth } from "../auth/AuthProvider";
import { AgentProgress } from "./AgentProgress";
import { EvidencePlayer } from "./EvidencePlayer";
import { explainHit, readableTranscript } from "./explain";
import { useAgentConversation } from "./useAgentConversation";
import styles from "./search.module.css";

type MessageOptionOverrides = Omit<AgentMessageRunOptions, "topK">;
type SelectedMessageOptions = Required<MessageOptionOverrides>;
/** Dispatched on window to focus the standalone search composer (⌘K / Ctrl K). */
export const FOCUS_SEARCH_EVENT = "seekino:focus-search";

const EXAMPLE_QUESTIONS = ["下雨天有人在门口等待的画面", "乐队第一次站上舞台演出", "两个人在天台吵架的那一段"];
const EMBEDDED_EXAMPLE_QUESTIONS = ["刚才发生了什么？", "现在画面里的人是谁？", "前面有类似的场景吗？"];

type SearchPageProps = {
  embedded?: boolean;
  initialSeriesId?: string;
  captureExtraContext?: () => Record<string, unknown> | null;
  onOpenVideo?: (target: { seriesId: string; videoId: string; startSeconds: number }) => void;
  /** Seeks the episode being watched; enables clickable timestamps in answers. Milliseconds. */
  onSeekCurrent?: (positionMs: number) => void;
};

export interface SearchPageHandle {
  focusComposer(): void;
}
const messageOptionsStorageKey = () => identityStorageKey("anivag-agent-message-options");
type StoredMessageOptions = { overrides?: MessageOptionOverrides; serverOptions?: AgentMessageOptions; selection?: SelectedMessageOptions };
const messageOptionNames = ["maxLoopTurns", "maxModelCallsPerTurn", "maxToolCallsPerTurn"] as const;

function defaultMessageOptions(options: AgentMessageOptions): SelectedMessageOptions {
  return {
    maxLoopTurns: 0,
    maxModelCallsPerTurn: options.maxModelCallsPerTurn.defaultValue,
    maxToolCallsPerTurn: options.maxToolCallsPerTurn.defaultValue,
  };
}

function boundedMessageOptionOverrides(overrides: MessageOptionOverrides, options: AgentMessageOptions): MessageOptionOverrides {
  const bounded: MessageOptionOverrides = {};
  for (const name of messageOptionNames) {
    const value = overrides[name];
    const limits = options[name];
    if (typeof value === "number" && Number.isInteger(value)
      && value >= limits.minimum && value <= limits.maximum) {
      bounded[name] = value;
    }
  }
  return bounded;
}

function storedMessageOptionOverrides(options: AgentMessageOptions): MessageOptionOverrides {
  try {
    const saved = JSON.parse(localStorage.getItem(messageOptionsStorageKey()) ?? "null") as StoredMessageOptions | null;
    if (saved?.overrides) return boundedMessageOptionOverrides(saved.overrides, options);
    if (!saved?.serverOptions || !saved.selection) return {};
    const previousDefaults = defaultMessageOptions(saved.serverOptions);
    const previousOverrides: MessageOptionOverrides = {};
    for (const name of messageOptionNames) {
      if (saved.selection[name] !== previousDefaults[name]) previousOverrides[name] = saved.selection[name];
    }
    return boundedMessageOptionOverrides(previousOverrides, options);
  } catch {
    return {};
  }
}

function messageRenderBlocks(message: AgentMessageView): AgentRenderBlock[] {
  const blocks = Array.isArray(message.renderBlocks) && message.renderBlocks.length > 0
    ? message.renderBlocks
    : message.content
      ? [{ type: "markdown" as const, markdown: message.content }]
      : [];
  const hitsById = new Map(
    message.searchResult?.hits.map((hit) => [hit.evidence.videoChunkId, hit]) ?? [],
  );
  return blocks.filter((block) => {
    if (block.type === "markdown") return Boolean(block.markdown.trim());
    const hit = hitsById.get(block.videoChunkId);
    return Boolean(
      hit
      && Number.isSafeInteger(block.startSeconds)
      && Number.isSafeInteger(block.endSeconds)
      && hit.evidence.startSeconds <= block.startSeconds
      && block.startSeconds < block.endSeconds
      && block.endSeconds <= hit.evidence.endSeconds,
    );
  });
}

async function loadConversations(): Promise<AgentConversationView[]> {
  const conversations: AgentConversationView[] = [];
  let cursor: string | undefined;
  while (true) {
    const page = await listAgentConversations(100, cursor);
    conversations.push(...page.items);
    if (!page.hasMore) return conversations;
    if (!page.nextCursor) throw new Error("对话列表缺少下一页游标");
    cursor = page.nextCursor;
  }
}

function conversationTime(conversation: AgentConversationView): string {
  return new Date(conversation.lastMessageAt ?? conversation.updatedAt).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function conversationAccessibleName(conversation: AgentConversationView): string {
  return `${conversation.title}，${conversationTime(conversation)}，编号 ${conversation.conversationId}`;
}

function linkTimestamps(children: ReactNode, onSeek: ((positionMs: number) => void) | undefined, displayMode: DisplayMode): ReactNode {
  const displayText = displayMode === "debug" ? (value: string) => value : formatTimeText;
  return Children.map(children, (child) => {
    if (typeof child !== "string") return child;
    if (!onSeek) return displayText(child);
    const parts: ReactNode[] = [];
    let cursor = 0;
    for (const match of child.matchAll(MEDIA_TIMESTAMP_PATTERN)) {
      const positionMs = timestampMilliseconds(match);
      if (positionMs === null) continue;
      parts.push(displayText(child.slice(cursor, match.index)));
      parts.push(<button key={match.index} type="button" className={styles.timeLink} onClick={() => onSeek(positionMs)} title={`跳转到 ${formatTimecode(positionMs)}`}>{displayText(match[0])}</button>);
      cursor = match.index! + match[0].length;
    }
    if (!parts.length) return displayText(child);
    parts.push(displayText(child.slice(cursor)));
    return parts;
  });
}

const SeekContext = createContext<{ onSeek?: (positionMs: number) => void; displayMode: DisplayMode }>({ displayMode: "release" });

function WithTimes({ children, link = true }: { children: ReactNode; link?: boolean }) {
  const { onSeek, displayMode } = useContext(SeekContext);
  return linkTimestamps(children, link ? onSeek : undefined, displayMode);
}

// Stable component identities: inline overrides would remount the answer on every render.
const answerComponents: Components = {
  h1: ({ children }) => <h1><WithTimes>{children}</WithTimes></h1>,
  h2: ({ children }) => <h2><WithTimes>{children}</WithTimes></h2>,
  h3: ({ children }) => <h3><WithTimes>{children}</WithTimes></h3>,
  h4: ({ children }) => <h4><WithTimes>{children}</WithTimes></h4>,
  h5: ({ children }) => <h5><WithTimes>{children}</WithTimes></h5>,
  h6: ({ children }) => <h6><WithTimes>{children}</WithTimes></h6>,
  p: ({ children }) => <p><WithTimes>{children}</WithTimes></p>,
  li: ({ children }) => <li><WithTimes>{children}</WithTimes></li>,
  strong: ({ children }) => <strong><WithTimes>{children}</WithTimes></strong>,
  em: ({ children }) => <em><WithTimes>{children}</WithTimes></em>,
  td: ({ children }) => <td><WithTimes>{children}</WithTimes></td>,
  th: ({ children }) => <th><WithTimes>{children}</WithTimes></th>,
  a: ({ children, href, title }) => href ? <a href={href} title={title} target="_blank" rel="noopener noreferrer"><WithTimes link={false}>{children}</WithTimes></a> : <span><WithTimes>{children}</WithTimes></span>,
  img: ({ alt }) => <span className={styles.answerImageBlocked}>{alt ? `[图片：${alt}]` : "[图片]"}</span>,
  table: ({ children }) => <div className={styles.answerTable}><table>{children}</table></div>,
};

function MarkdownAnswer({ markdown }: { markdown: string }) {
  return (
    <div className={styles.answerText}>
      <Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={defaultUrlTransform} components={answerComponents}>{markdown}</Markdown>
    </div>
  );
}

export const SearchPage = forwardRef<SearchPageHandle, SearchPageProps>(function SearchPage(
  { embedded = false, initialSeriesId, captureExtraContext, onOpenVideo, onSeekCurrent },
  ref,
) {
  const { mode, displayMode, series, isLoading: workspaceLoading } = useWorkspace();
  const { canManage } = useAuth();
  const inRouter = useInRouterContext();
  const [requestedConversationId, setRequestedConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<AgentConversationView[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(true);
  const [conversationListError, setConversationListError] = useState<string | null>(null);
  const [conversationActionError, setConversationActionError] = useState<string | null>(null);
  const [creatingConversation, setCreatingConversation] = useState(false);
  const [deletingConversationId, setDeletingConversationId] = useState<string | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [conversationStatus, setConversationStatus] = useState("");
  const [playbackContextError, setPlaybackContextError] = useState<string | null>(null);
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<string[]>([]);
  const scopeTouchedRef = useRef(false);
  const [query, setQuery] = useState("");
  const [reference, setReference] = useState<EvidenceReference | null>(null);
  const [messageOptionConfig, setMessageOptionConfig] = useState<AgentMessageOptions | null>(null);
  const [messageOptionOverrides, setMessageOptionOverrides] = useState<MessageOptionOverrides | null>(null);
  const [messageOptionsError, setMessageOptionsError] = useState<string | null>(null);
  const customLoopTurnsRef = useRef(100);
  const messageOptionsRequestRef = useRef(0);
  const messageOptions = messageOptionConfig && messageOptionOverrides
    ? { ...defaultMessageOptions(messageOptionConfig), ...messageOptionOverrides }
    : null;
  const messageOptionSummary = !messageOptions || !messageOptionOverrides ? "服务端默认" : [
    messageOptionOverrides.maxLoopTurns === undefined ? null : `最多 ${messageOptions.maxLoopTurns} 轮`,
  ].filter(Boolean).join(" · ") || (Object.keys(messageOptionOverrides).length ? "已自定义" : "服务端默认");
  const [selection, setSelection] = useState<{ conversationId: string; messageId: string; chunkId: string; startSeconds: number; endSeconds: number } | null>(null);
  const deleteWarningId = useId();
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const conversationPickerRef = useRef<HTMLDetailsElement>(null);
  const scopePickerRef = useRef<HTMLDetailsElement>(null);
  const answerSettingsRef = useRef<HTMLDetailsElement>(null);
  const deleteCancelRef = useRef<HTMLButtonElement>(null);
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null);
  const conversationFailureRef = useRef<HTMLParagraphElement>(null);
  const conversationsRef = useRef<AgentConversationView[]>([]);
  const deletedConversationIdsRef = useRef(new Set<string>());
  const focusAfterMutationRef = useRef<"composer" | "picker" | null>(null);
  const conversationListRequestRef = useRef(0);
  const wasSearchingRef = useRef(false);
  const threadRef = useRef<HTMLDivElement>(null);
  const followThread = useRef(true);

  useImperativeHandle(ref, () => ({
    focusComposer: () => composerRef.current?.focus({ preventScroll: true }),
  }), []);
  useEffect(() => {
    if (embedded) return;
    const focus = () => composerRef.current?.focus({ preventScroll: true });
    if ((window.history.state as { usr?: { focusComposer?: boolean } } | null)?.usr?.focusComposer) focus();
    window.addEventListener(FOCUS_SEARCH_EVENT, focus);
    return () => window.removeEventListener(FOCUS_SEARCH_EVENT, focus);
  }, [embedded]);
  const { conversationId, messages, progress, loading, searching, cancelling = false, error, clear: clearConversation, send, stop } = useAgentConversation(requestedConversationId);
  useReportAssistantBusy(searching || messages.some((message) => message.status === "PROCESSING"));
  const pendingExampleRef = useRef(false);
  useEffect(() => {
    if (!pendingExampleRef.current) return;
    pendingExampleRef.current = false;
    composerRef.current?.form?.requestSubmit();
  }, [query]);
  const isSearchable = (status: string) => status === "READY";
  const initialSeries = initialSeriesId ? series.find((item) => item.seriesId === initialSeriesId) : null;
  const scopeValid = selectedSeriesIds.every((id) => series.some((item) => item.seriesId === id && isSearchable(item.status)));
  const canSend = Boolean(query.trim() && !loading && !searching && conversationId && !deletedConversationIdsRef.current.has(conversationId) && scopeValid && mode === "live");

  useEffect(() => {
    if (!embedded || !initialSeriesId || scopeTouchedRef.current) return;
    const next = initialSeries?.status === "READY" ? [initialSeriesId] : [];
    setSelectedSeriesIds((current) => current.length === next.length && current.every((id, index) => id === next[index]) ? current : next);
  }, [embedded, initialSeriesId, initialSeries?.status]);

  useLayoutEffect(() => {
    const composer = composerRef.current;
    if (!composer) return;
    composer.style.height = "auto";
    composer.style.height = `${composer.scrollHeight}px`;
  }, [query]);

  const evidenceMessage = selection?.conversationId === conversationId ? messages.find((message) => message.messageId === selection.messageId) : null;
  const selectedHit = evidenceMessage?.searchResult?.hits.find((hit) => hit.evidence.videoChunkId === selection?.chunkId) ?? null;
  const referenceMessage = reference ? messages.find((message) => message.messageId === reference.messageId) : null;
  const referenceHit = referenceMessage?.searchResult?.hits.find((hit) => hit.evidence.videoChunkId === reference?.videoChunkId) ?? null;
  const referenceBlock = referenceMessage && reference ? messageRenderBlocks(referenceMessage).find((block) => block.type === "video" && block.videoChunkId === reference.videoChunkId) : null;
  const referenceRange = referenceHit && referenceBlock?.type === "video"
    ? { startSeconds: referenceBlock.startSeconds, endSeconds: referenceBlock.endSeconds }
    : null;
  const activeConversation = conversations.find((item) => item.conversationId === conversationId) ?? null;

  useEffect(() => {
    if (displayMode !== "debug") return;
    let active = true;
    const refresh = () => {
      const requestId = ++messageOptionsRequestRef.current;
      void getAgentMessageOptions().then((options) => {
        if (!active || requestId !== messageOptionsRequestRef.current) return;
        setMessageOptionOverrides((current) => {
          const overrides = current === null
            ? storedMessageOptionOverrides(options)
            : boundedMessageOptionOverrides(current, options);
          if (overrides.maxLoopTurns) customLoopTurnsRef.current = overrides.maxLoopTurns;
          else if (current === null || customLoopTurnsRef.current < options.maxLoopTurns.minimum || customLoopTurnsRef.current > options.maxLoopTurns.maximum) {
            customLoopTurnsRef.current = options.maxLoopTurns.defaultValue;
          }
          return overrides;
        });
        setMessageOptionConfig(options);
        setMessageOptionsError(null);
      }).catch((failure) => {
        if (active && requestId === messageOptionsRequestRef.current) setMessageOptionsError(errorMessage(failure));
      });
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => { active = false; window.removeEventListener("focus", refresh); };
  }, [displayMode]);

  useEffect(() => {
    if (!messageOptionOverrides) return;
    try {
      localStorage.setItem(messageOptionsStorageKey(), JSON.stringify({ overrides: messageOptionOverrides }));
    } catch { /* Browser storage is optional; the current tab still keeps the selection. */ }
  }, [messageOptionOverrides]);

  const replaceConversations = useCallback((items: AgentConversationView[]) => {
    conversationsRef.current = items;
    setConversations(items);
  }, []);

  const refreshConversations = useCallback(async (showLoading = false) => {
    const requestId = ++conversationListRequestRef.current;
    if (showLoading) setConversationsLoading(true);
    try {
      const items = await loadConversations();
      if (conversationListRequestRef.current !== requestId) return;
      replaceConversations(items);
      setConversationListError(null);
    } catch (failure) {
      if (conversationListRequestRef.current === requestId) setConversationListError(`读取对话记录失败：${errorMessage(failure)}`);
    } finally {
      if (conversationListRequestRef.current === requestId) setConversationsLoading(false);
    }
  }, [replaceConversations]);

  useEffect(() => { setSelection(null); setReference(null); }, [conversationId]);

  useEffect(() => {
    if (confirmingDeleteId) deleteCancelRef.current?.focus();
  }, [confirmingDeleteId]);

  useEffect(() => {
    const target = focusAfterMutationRef.current;
    if (target === "composer" && !loading && conversationId) {
      focusAfterMutationRef.current = null;
      setConversationStatus("对话已打开");
      composerRef.current?.focus();
    } else if (target === "composer" && !loading && !conversationId) {
      focusAfterMutationRef.current = null;
      setConversationStatus(error ? `打开对话失败：${error}` : "打开对话失败");
      if (conversationFailureRef.current) conversationFailureRef.current.focus();
      else conversationPickerRef.current?.querySelector<HTMLElement>("summary")?.focus();
    } else if (target === "picker" && !creatingConversation && !deletingConversationId) {
      focusAfterMutationRef.current = null;
      conversationPickerRef.current?.querySelector<HTMLElement>("summary")?.focus();
    }
  }, [conversationId, creatingConversation, deletingConversationId, error, loading]);

  useEffect(() => { void refreshConversations(true); }, [refreshConversations]);

  useEffect(() => {
    if (conversationId) void refreshConversations();
  }, [conversationId, refreshConversations]);

  useEffect(() => {
    if (conversationId && messages.length > 0) void refreshConversations();
  }, [conversationId, messages.length, refreshConversations]);

  useEffect(() => {
    const wasSearching = wasSearchingRef.current;
    wasSearchingRef.current = searching;
    if (wasSearching && !searching && conversationId) {
      void refreshConversations();
      const latestAssistant = [...messages].reverse().find((message) => message.role === "ASSISTANT");
      setConversationStatus(latestAssistant?.status === "COMPLETED" && !error ? "回答已完成" : "回答未完成");
    }
  }, [conversationId, error, messages, refreshConversations, searching]);

  useEffect(() => {
    if (followThread.current && threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight;
  }, [messages, searching]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSend) return;
    const extraContext = captureExtraContext?.() ?? null;
    if (captureExtraContext && extraContext === null) {
      setPlaybackContextError("当前播放位置尚未就绪，请稍后重试");
      return;
    }
    setPlaybackContextError(null);
    followThread.current = true;
    setConversationStatus("问题已发送，正在处理");
    const options = displayMode === "debug" && messageOptionOverrides
      ? { ...messageOptionOverrides, maxLoopTurns: messageOptionOverrides.maxLoopTurns || undefined }
      : {};
    if (extraContext === null) void send(query, selectedSeriesIds, reference, options);
    else void send(query, selectedSeriesIds, reference, options, extraContext);
    setQuery("");
    setReference(null);
  };

  const closeConversationPicker = (restoreFocus = false) => {
    conversationPickerRef.current?.removeAttribute("open");
    if (restoreFocus) conversationPickerRef.current?.querySelector<HTMLElement>("summary")?.focus();
  };

  const selectConversation = (conversation: AgentConversationView) => {
    if (creatingConversation || deletingConversationId) return;
    const nextConversationId = conversation.conversationId;
    setConfirmingDeleteId(null);
    setConversationActionError(null);
    closeConversationPicker();
    if (nextConversationId === conversationId) {
      composerRef.current?.focus();
      return;
    }
    followThread.current = true;
    focusAfterMutationRef.current = "composer";
    setConversationStatus(`正在打开对话：${conversation.title}`);
    setRequestedConversationId(nextConversationId);
  };

  const createConversation = async () => {
    if (mode !== "live" || loading || creatingConversation || deletingConversationId) return;
    const currentConversationCanBeReused = Boolean(
      !error
      && conversationId
      && !deletedConversationIdsRef.current.has(conversationId)
      && messages.length === 0
      && (!activeConversation || activeConversation.messageCount === 0),
    );
    if (currentConversationCanBeReused) {
      closeConversationPicker();
      composerRef.current?.focus();
      return;
    }
    setCreatingConversation(true);
    setConfirmingDeleteId(null);
    setConversationActionError(null);
    try {
      const conversation = await createAgentConversation(crypto.randomUUID());
      deletedConversationIdsRef.current.delete(conversation.conversationId);
      replaceConversations([conversation, ...conversationsRef.current.filter((item) => item.conversationId !== conversation.conversationId)]);
      followThread.current = true;
      focusAfterMutationRef.current = "composer";
      setRequestedConversationId(conversation.conversationId);
      setConversationStatus("已新建对话");
      closeConversationPicker();
    } catch (failure) {
      setConversationActionError(`新建对话失败：${errorMessage(failure)}`);
      if (conversationPickerRef.current) conversationPickerRef.current.open = true;
    } finally {
      setCreatingConversation(false);
    }
  };

  const deleteConversation = async (conversation: AgentConversationView) => {
    const deletingCurrentConversation = conversation.conversationId === conversationId;
    if (mode !== "live" || loading || creatingConversation || deletingConversationId || (deletingCurrentConversation && searching)) return;
    setDeletingConversationId(conversation.conversationId);
    setConversationActionError(null);
    let deleted = false;
    try {
      await deleteAgentConversation(conversation.conversationId);
      deleted = true;
      deletedConversationIdsRef.current.add(conversation.conversationId);
      conversationListRequestRef.current += 1;
      setConversationsLoading(false);
      let remaining = conversationsRef.current.filter((item) => item.conversationId !== conversation.conversationId);
      replaceConversations(remaining);
      setConfirmingDeleteId(null);
      setConversationStatus(`已删除对话：${conversation.title}`);
      if (deletingCurrentConversation) {
        clearConversation();
        let next = remaining[0];
        if (!next) {
          next = await createAgentConversation(crypto.randomUUID());
          deletedConversationIdsRef.current.delete(next.conversationId);
          remaining = [next];
          replaceConversations(remaining);
          setConversationStatus(`已删除对话：${conversation.title}；已创建新的空对话`);
        }
        followThread.current = true;
        focusAfterMutationRef.current = "composer";
        setRequestedConversationId(next.conversationId);
        closeConversationPicker();
      } else {
        focusAfterMutationRef.current = "picker";
        void refreshConversations();
      }
    } catch (failure) {
      setConversationActionError(`${deleted ? "对话已删除，但新对话创建失败" : "删除对话失败"}：${errorMessage(failure)}`);
      if (!deleted) setConfirmingDeleteId(conversation.conversationId);
      if (deleted) focusAfterMutationRef.current = "picker";
      if (conversationPickerRef.current) conversationPickerRef.current.open = true;
    } finally {
      setDeletingConversationId(null);
    }
  };

  const Root = embedded ? "section" : "main";
  return (
    <Root className={`${styles.page} ${embedded ? styles.embeddedPage : "page"}`}>
      <header className={styles.pageHeader}>
        <div className={styles.chatHeading}>
          {embedded ? (
            <h2 className={styles.chatTitle} title={activeConversation?.title ?? "当前对话"}>
              {activeConversation?.title ?? "当前对话"}
            </h2>
          ) : (
            <>
              <h1 className={styles.chatTitle}>Seekino 助手</h1>
              {activeConversation ? <span title={activeConversation.title}>{activeConversation.title}</span> : null}
            </>
          )}
        </div>
        <div className={styles.conversationActions}>
          <p className="sr-only" role="status" aria-live="polite">{conversationStatus}</p>
          <details
            className={styles.conversationPicker}
            ref={conversationPickerRef}
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              if (confirmingDeleteId) {
                setConfirmingDeleteId(null);
                deleteTriggerRef.current?.focus();
                return;
              }
              closeConversationPicker(true);
            }}
          >
            <summary aria-label="打开对话记录">
              <History size={15} aria-hidden="true" />
              <span>对话记录</span>
              <ChevronDown size={13} aria-hidden="true" />
            </summary>
            <section className={styles.conversationMenu} aria-label="对话记录">
              <header>
                <div><strong>对话记录</strong><small>{conversations.length ? `${conversations.length} 个对话` : "暂无记录"}</small></div>
                {conversationsLoading ? <LoaderCircle size={14} className={styles.spinning} aria-label="正在读取对话记录" /> : null}
                <button className={styles.conversationMenuClose} type="button" onClick={() => closeConversationPicker(true)} aria-label="关闭对话记录">
                  <X size={14} aria-hidden="true" />
                </button>
              </header>
              {conversationActionError || conversationListError ? <p className={styles.conversationError} role="alert">{conversationActionError ?? conversationListError}</p> : null}
              <ul className={styles.conversationList}>
                {conversations.map((conversation) => {
                  const current = conversation.conversationId === conversationId;
                  const deleting = deletingConversationId === conversation.conversationId;
                  const cannotDelete = current && searching;
                  const mutationLocked = loading || creatingConversation || Boolean(deletingConversationId);
                  const accessibleName = conversationAccessibleName(conversation);
                  return (
                    <li key={conversation.conversationId} className={current ? styles.conversationCurrent : undefined}>
                      <button
                        className={styles.conversationChoice}
                        type="button"
                        onClick={() => selectConversation(conversation)}
                        aria-current={current ? "true" : undefined}
                        aria-label={`打开对话 ${accessibleName}`}
                        disabled={creatingConversation || Boolean(deletingConversationId)}
                      >
                        <span><strong>{conversation.title}</strong><time>{conversationTime(conversation)}</time></span>
                        <small>{conversation.lastMessagePreview || (conversation.messageCount ? `${conversation.messageCount} 条消息` : "还没有消息")}</small>
                      </button>
                      <button
                        className={styles.deleteConversation}
                        type="button"
                        onClick={(event) => {
                          deleteTriggerRef.current = event.currentTarget;
                          setConfirmingDeleteId(conversation.conversationId);
                        }}
                        disabled={mode !== "live" || mutationLocked || cannotDelete}
                        aria-label={`删除对话 ${accessibleName}`}
                        title={cannotDelete ? "当前回答进行中，完成后才能删除" : mutationLocked ? "请等待当前对话操作完成" : "删除对话"}
                      >
                        {deleting ? <LoaderCircle size={14} className={styles.spinning} aria-hidden="true" /> : <Trash2 size={14} aria-hidden="true" />}
                      </button>
                      {cannotDelete ? <small className={styles.deleteHint}>回答完成后可删除</small> : null}
                      {confirmingDeleteId === conversation.conversationId ? (
                        <div className={styles.deleteConfirmation} role="group" aria-label={`确认删除对话 ${accessibleName}`} aria-describedby={deleteWarningId}>
                          <p id={deleteWarningId}>删除后，这个对话及其中的全部消息都无法恢复。</p>
                          <div>
                            <button ref={deleteCancelRef} type="button" aria-describedby={deleteWarningId} onClick={() => {
                              setConfirmingDeleteId(null);
                              deleteTriggerRef.current?.focus();
                            }}>取消</button>
                            <button
                              type="button"
                              disabled={mutationLocked || cannotDelete}
                              onClick={() => void deleteConversation(conversation)}
                              aria-label={`确认删除对话 ${accessibleName}`}
                              aria-describedby={deleteWarningId}
                              title={cannotDelete ? "当前回答进行中，完成后才能删除" : undefined}
                            >确认删除</button>
                          </div>
                        </div>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
              {!conversationsLoading && !conversations.length ? <p className={styles.conversationEmpty}>还没有可显示的对话。</p> : null}
            </section>
          </details>
          <button
            className={styles.newConversation}
            type="button"
            onClick={() => void createConversation()}
            disabled={mode !== "live" || loading || creatingConversation || Boolean(deletingConversationId)}
            aria-label="新建对话"
          >
            {creatingConversation ? <LoaderCircle size={15} className={styles.spinning} aria-hidden="true" /> : <Plus size={15} aria-hidden="true" />}
            <span>{creatingConversation ? "正在新建" : "新对话"}</span>
          </button>
        </div>
      </header>

      <div className={selectedHit && !onOpenVideo ? `${styles.workspace} ${styles.withEvidence}` : styles.workspace}>
        <section className={styles.agentPanel} aria-label="与 Seekino 助手对话">
          <div className={styles.thread} ref={threadRef} onScroll={(event) => {
            const thread = event.currentTarget;
            followThread.current = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
          }}>
            {!messages.length && !loading ? (
              <div className={styles.welcome}>
                <Mascot size={embedded ? 88 : 140} />
                <p>{embedded ? "看到哪里，问到哪里。回答里的时间点可以直接点击跳转。" : "描述记得的画面、台词或人物关系，一起把那一刻找回来。"}</p>
                <div className={styles.examples} aria-label="试试这样问">
                  {(embedded ? EMBEDDED_EXAMPLE_QUESTIONS : EXAMPLE_QUESTIONS).map((example) => <button key={example} type="button" disabled={mode !== "live" || !conversationId} onClick={() => {
                    pendingExampleRef.current = true;
                    setQuery(example);
                  }}>{example}</button>)}
                </div>
              </div>
            ) : null}
            {messages.map((message) => (
              <article key={message.messageId} className={message.role === "USER" ? styles.userMessage : styles.agentMessage}>
                {message.role === "USER" ? (
                  <div>
                    <p>{message.content}</p>
                    {message.seriesIds.length ? <small className={styles.messageScope}>可用视频范围：{message.seriesIds.map((id) => series.find((item) => item.seriesId === id)?.name ?? id).join("、")}</small> : null}
                  </div>
                ) : (
                  <div>
                    <AgentProgress message={message} steps={progress[message.messageId] ?? []} displayMode={displayMode} />
                    {message.status === "FAILED" ? <p className={styles.messageError} role="alert">{message.lastError?.message || "本次回答未完成"}</p> : null}
                    {!message.content && message.status === "COMPLETED" && !message.searchResult ? <p className={styles.messageError}>未生成回答</p> : null}
                    {displayMode === "debug" && message.searchResult?.warnings.length ? <div className={styles.resultWarnings} role="status">{message.searchResult.warnings.map((warning) => <p key={warning}>{warning}</p>)}</div> : message.searchResult?.partial ? <p className={styles.resultWarnings} role="status">本次回答仅覆盖当前可检索的视频。</p> : null}
                    {messageRenderBlocks(message).map((block, index, blocks) => {
                      if (block.type === "markdown") return <SeekContext.Provider key={`markdown:${index}`} value={{ onSeek: onSeekCurrent, displayMode }}><MarkdownAnswer markdown={block.markdown} /></SeekContext.Provider>;
                      const hit = message.searchResult?.hits.find((item) => item.evidence.videoChunkId === block.videoChunkId);
                      if (!hit) return null;
                      const explanation = explainHit(hit);
                      const playbackRange = { startSeconds: block.startSeconds, endSeconds: block.endSeconds };
                      const selected = selection?.messageId === message.messageId && selectedHit?.evidence.videoChunkId === hit.evidence.videoChunkId;
                      const thumbnailUrl = hit.evidence.thumbnail ? mediaUrl(hit.evidence.thumbnail.url) : null;
                      const firstVideo = !blocks.slice(0, index).some((item) => item.type === "video" && message.searchResult?.hits.some((candidate) => candidate.evidence.videoChunkId === item.videoChunkId));
                      return (
                        <section key={`video:${block.videoChunkId}:${playbackRange.startSeconds}:${playbackRange.endSeconds}:${index}`} className={`${styles.candidatesPanel} ${styles.renderVideoBlock}`} aria-label="检索助手提交的视频片段">
                          {firstVideo ? <div className={styles.candidateHeading}><h2>视频片段</h2><span>{message.searchResult?.hits.length ?? 0} 段</span></div> : null}
                          <div className={styles.candidateList}>
                            <button
                              type="button"
                              className={selected ? styles.candidateSelected : styles.candidate}
                              onClick={() => {
                                if (onOpenVideo) {
                                  onOpenVideo({ seriesId: hit.evidence.seriesId, videoId: hit.evidence.videoId, startSeconds: playbackRange.startSeconds });
                                } else {
                                  setSelection({ conversationId: message.conversationId, messageId: message.messageId, chunkId: hit.evidence.videoChunkId, ...playbackRange });
                                }
                              }}
                              aria-pressed={selected}
                              title={onOpenVideo ? "打开整集并定位到片段" : undefined}
                            >
                              <div className={styles.candidateTop}>
                                <span className={styles.rank}>{hit.candidate.rank}</span>
                                <span>{hit.evidence.seriesName} · {episodeLabel(hit.evidence.episodeOrder)}</span>
                                <time>{formatTimeRange(playbackRange.startSeconds, playbackRange.endSeconds)}</time>
                              </div>
                              {thumbnailUrl ? (
                                <img className={styles.candidateThumbnail} src={thumbnailUrl} alt={hit.evidence.thumbnail?.alt || `${hit.evidence.seriesName} 视频片段首帧`} loading="lazy" />
                              ) : (
                                <span className={displayMode === "debug" ? styles.candidateThumbnailFallback : styles.whyLine} role="img" aria-label={displayMode === "debug" ? "该片段暂无首帧缩略图" : "播放所选片段"}>
                                  <Play size={22} fill="currentColor" aria-hidden="true" />
                                  <span>{hit.evidence.playbackStatus === "READY" ? displayMode === "debug" ? "暂无首帧 · 点击加载视频" : "播放片段" : "视频暂不可播放"}</span>
                                </span>
                              )}
                              <strong>{displayMode === "debug" ? explanation.summary : formatTimeText(explanation.summary)}</strong>
                              {displayMode === "debug" ? <>
                                <p>{hit.evidence.videoCompact || hit.evidence.windowSummary}</p>
                                <blockquote>{readableTranscript(hit)}</blockquote>
                              </> : null}
                            </button>
                          </div>
                        </section>
                      );
                    })}
                    {message.searchResult && !message.searchResult.hits.length ? (
                      <p className={styles.candidateEmpty}>
                        <Mascot mood="puzzled" size={44} />
                        <span>{message.searchResult.retrievalTrace?.some((entry) => entry.resultCount > 0)
                          ? "检索到了候选片段，但本次回答未附带可播放视频，请重试。"
                          : "没有检索到可展示的视频片段，试试换个描述。"}</span>
                      </p>
                    ) : null}
                  </div>
                )}
              </article>
            ))}
            {loading || (searching && !messages.some((message) => message.status === "PROCESSING")) ? <p className={styles.connectionState} role="status"><Mascot mood="seek" size={36} delayed />{loading ? "正在恢复会话…" : "正在连接助手…"}</p> : null}
            {error && !messages.some((message) => message.status === "FAILED" && message.lastError?.message === error) ? <p ref={conversationFailureRef} className={`${styles.messageError} ${styles.conversationError}`} role="alert" tabIndex={-1}><Mascot mood="oops" size={36} /><span>{error}</span></p> : null}
          </div>

          <div className={styles.composerArea}>
            <form className={styles.composer} onSubmit={submit}>
              {reference ? <div className={styles.referenceChip}><span>已引用{referenceHit && referenceRange ? ` ${referenceHit.evidence.seriesName} · ${episodeLabel(referenceHit.evidence.episodeOrder)} · ${formatTimeRange(referenceRange.startSeconds, referenceRange.endSeconds)}` : "选中片段"}</span><button type="button" onClick={() => setReference(null)} aria-label="取消引用"><X size={13} /></button></div> : null}
              <textarea
                ref={composerRef}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                placeholder="提问视频内容，或聊聊其他问题…"
                rows={2}
                aria-label="向 Seekino 助手提问"
              />
              {searching ? (
                <button className={styles.sendButton} type="button" disabled={cancelling || !messages.some((message) => message.role === "ASSISTANT" && message.status === "PROCESSING")} onClick={() => void stop?.()} aria-label={cancelling ? "正在停止" : "停止生成"} title={cancelling ? "正在停止" : "停止生成"}>
                  {cancelling ? <LoaderCircle size={18} className={styles.spinning} aria-hidden="true" /> : <Square size={16} aria-hidden="true" />}
                </button>
              ) : (
                <button className={styles.sendButton} type="submit" disabled={!canSend} aria-label="发送问题">
                  <ArrowUp size={19} aria-hidden="true" />
                </button>
              )}
            </form>
            {playbackContextError ? <p className={styles.messageError} role="alert">{playbackContextError}</p> : null}
            <div className={styles.composerToolbar}>
              <div className={styles.composerControls}>
                <details
                  className={styles.scopePicker}
                  ref={scopePickerRef}
                  onToggle={(event) => {
                    if (event.currentTarget.open) answerSettingsRef.current?.removeAttribute("open");
                  }}
                >
                  <summary aria-label="选择检索视频系列">{embedded && selectedSeriesIds.length === 1
                    ? series.find((item) => item.seriesId === selectedSeriesIds[0])?.name ?? "已选 1 个系列"
                    : selectedSeriesIds.length ? `已选 ${selectedSeriesIds.length} 个系列` : "全部视频系列"}<ChevronDown size={13} aria-hidden="true" /></summary>
                  <div className={styles.scopeOptions} role="group" aria-label="检索视频系列多选">
                    <button type="button" onClick={() => { scopeTouchedRef.current = true; setSelectedSeriesIds([]); }}>检索全部视频系列</button>
                    {series.map((item) => (
                      <label key={item.seriesId}>
                        <input
                          type="checkbox"
                          checked={selectedSeriesIds.includes(item.seriesId)}
                          disabled={!isSearchable(item.status) && !selectedSeriesIds.includes(item.seriesId)}
                          onChange={(event) => {
                            scopeTouchedRef.current = true;
                            setSelectedSeriesIds((current) => event.target.checked ? [...current, item.seriesId] : current.filter((id) => id !== item.seriesId));
                          }}
                        />
                        <span>{item.name}</span>
                        <small>{isSearchable(item.status) ? `${item.readyVideoCount} 个视频可检索` : "暂不可检索"}</small>
                      </label>
                    ))}
                    {!series.length ? <p>资料库还没有视频系列</p> : null}
                  </div>
                </details>
                {displayMode === "debug" ? <details
                  className={`${styles.scopePicker} ${styles.answerSettingsPicker}`}
                  ref={answerSettingsRef}
                  onToggle={(event) => {
                    if (event.currentTarget.open) scopePickerRef.current?.removeAttribute("open");
                  }}
                >
                  <summary aria-label="配置本次回答">
                    <SlidersHorizontal size={13} aria-hidden="true" />
                    <span>回答设置</span>
                    <small>{messageOptionSummary}</small>
                    <ChevronDown size={13} aria-hidden="true" />
                  </summary>
                  <div className={`${styles.scopeOptions} ${styles.answerSettings}`} role="group" aria-label="本次回答设置">
                    <header>
                      <div><strong>本次回答</strong><small>自动使用全局运行限制；自定义可限制最多轮次。</small></div>
                      <button
                        type="button"
                        disabled={!messageOptionConfig || !messageOptions || searching}
                        onClick={() => {
                          if (!messageOptionConfig) return;
                          customLoopTurnsRef.current = messageOptionConfig.maxLoopTurns.defaultValue;
                          setMessageOptionOverrides({});
                        }}
                      >恢复默认</button>
                    </header>
                    {messageOptionConfig && messageOptions ? (
                      <>
                        <div className={styles.answerMode}>
                          <span><strong>运行轮次</strong><small>自定义最多可设 {messageOptionConfig.maxLoopTurns.maximum} 轮</small></span>
                          <div className={styles.answerModeChoices} role="group" aria-label="运行轮次模式">
                            <button type="button" aria-pressed={messageOptions.maxLoopTurns === 0} onClick={() => {
                              if (messageOptions.maxLoopTurns > 0) customLoopTurnsRef.current = messageOptions.maxLoopTurns;
                              setMessageOptionOverrides((current) => {
                                if (!current) return current;
                                const next = { ...current };
                                delete next.maxLoopTurns;
                                return next;
                              });
                            }}>自动</button>
                            <button type="button" aria-pressed={messageOptions.maxLoopTurns > 0} onClick={() => {
                              setMessageOptionOverrides((current) => current && ({ ...current, maxLoopTurns: customLoopTurnsRef.current }));
                            }}>自定义</button>
                          </div>
                        </div>
                        {messageOptions.maxLoopTurns > 0 ? (
                          <label className={styles.answerSettingRow}>
                            <span><strong>最多轮次</strong><small>检索与核验的循环上限</small></span>
                            <span className={styles.answerSettingValue}>{messageOptions.maxLoopTurns}</span>
                            <input
                              aria-label="最多运行轮次"
                              type="range"
                              min={messageOptionConfig.maxLoopTurns.minimum}
                              max={messageOptionConfig.maxLoopTurns.maximum}
                              value={messageOptions.maxLoopTurns}
                              onChange={(event) => {
                                const maxLoopTurns = event.currentTarget.valueAsNumber;
                                customLoopTurnsRef.current = maxLoopTurns;
                                setMessageOptionOverrides((current) => current && ({ ...current, maxLoopTurns }));
                              }}
                            />
                          </label>
                        ) : null}
                      </>
                    ) : <p role={messageOptionsError ? "alert" : "status"}>{messageOptionsError ? `暂时无法读取设置：${messageOptionsError}` : "正在读取服务端限制…"}</p>}
                  </div>
                </details> : null}
              </div>
              <span className={styles.keyboardHint}>Enter 发送 · Shift + Enter 换行</span>
            </div>
            {!scopeValid ? <p className={styles.messageError} role="alert">所选视频系列暂不可检索，请重新选择范围。</p> : null}
            {embedded && initialSeriesId && !workspaceLoading && initialSeries?.status !== "READY" ? (
              <p className={styles.scopeNotice} role="status">
                <span className={styles.scopeChip}>检索范围：{selectedSeriesIds.length ? selectedSeriesIds.map((id) => series.find((item) => item.seriesId === id)?.name ?? id).join("、") : "全部系列"}</span>
                <span className={styles.scopeWarning}><TriangleAlert size={13} aria-hidden="true" />本系列未完成解析</span>
                {canManage ? inRouter ? <Link to="/library">去解析</Link> : <a href="/library">去解析</a> : null}
              </p>
            ) : null}
          </div>
        </section>

        {!onOpenVideo && selectedHit && evidenceMessage && selection ? (
          <section className={styles.evidenceColumn} aria-label="片段详情" onKeyDown={(event) => {
            if (event.key === "Escape") setSelection(null);
          }}>
            <header className={styles.evidenceHeader}>
              <strong>片段详情</strong>
              <button type="button" onClick={() => setSelection(null)} aria-label="关闭片段详情"><X size={18} /></button>
            </header>
            <EvidencePlayer hit={selectedHit} displayMode={displayMode} playbackRange={{ startSeconds: selection.startSeconds, endSeconds: selection.endSeconds }} />
            <button className={`button-secondary ${styles.referenceButton}`} type="button" onClick={() => {
              setReference({ messageId: evidenceMessage.messageId, videoChunkId: selectedHit.evidence.videoChunkId });
              setSelection(null);
              composerRef.current?.focus();
            }}>引用此片段追问</button>
          </section>
        ) : null}
      </div>
    </Root>
  );
});
