import { ArrowUp, ChevronDown, History, LoaderCircle, MessageCircleMore, Play, Plus, ScanSearch, Trash2, X } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";
import Markdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

import { useWorkspace } from "../../app/workspace";
import { createAgentConversation, deleteAgentConversation, errorMessage, listAgentConversations, mediaUrl } from "../../api/client";
import type { AgentConversationView, AgentMessageView, AgentRenderBlock, EvidenceReference } from "../../api/contracts";
import { clipText, episodeLabel, formatTimeRange } from "../../shared/format";
import { AgentProgress } from "./AgentProgress";
import { EvidencePlayer } from "./EvidencePlayer";
import { explainHit, readableTranscript } from "./explain";
import { useAgentConversation } from "./useAgentConversation";
import styles from "./search.module.css";

function messageRenderBlocks(message: AgentMessageView): AgentRenderBlock[] {
  if (message.renderBlocks !== undefined) return message.renderBlocks;
  return [
    ...(message.content ? [{ type: "markdown" as const, markdown: message.content }] : []),
    ...(message.searchResult?.hits.map((hit) => ({
      type: "video" as const,
      videoChunkId: hit.evidence.videoChunkId,
      startMs: hit.evidence.startMs,
      endMs: hit.evidence.endMs,
    })) ?? []),
  ];
}

function videoPlaybackRange(block: Extract<AgentRenderBlock, { type: "video" }>, hit: NonNullable<AgentMessageView["searchResult"]>["hits"][number]) {
  if (
    Number.isSafeInteger(block.startMs)
    && Number.isSafeInteger(block.endMs)
    && hit.evidence.startMs <= block.startMs
    && block.startMs < block.endMs
    && block.endMs <= hit.evidence.endMs
  ) {
    return { startMs: block.startMs, endMs: block.endMs };
  }
  return { startMs: hit.evidence.startMs, endMs: hit.evidence.endMs };
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

function MarkdownAnswer({ markdown }: { markdown: string }) {
  return (
    <div className={styles.answerText}>
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        urlTransform={defaultUrlTransform}
        components={{
          a: ({ children, href, title }) => href ? <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
          img: ({ alt }) => <span className={styles.answerImageBlocked}>{alt ? `[图片：${alt}]` : "[图片]"}</span>,
          table: ({ children }) => <div className={styles.answerTable}><table>{children}</table></div>,
        }}
      >{markdown}</Markdown>
    </div>
  );
}

export function SearchPage() {
  const { mode, series } = useWorkspace();
  const [requestedConversationId, setRequestedConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<AgentConversationView[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(true);
  const [conversationListError, setConversationListError] = useState<string | null>(null);
  const [conversationActionError, setConversationActionError] = useState<string | null>(null);
  const [creatingConversation, setCreatingConversation] = useState(false);
  const [deletingConversationId, setDeletingConversationId] = useState<string | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [conversationStatus, setConversationStatus] = useState("");
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [reference, setReference] = useState<EvidenceReference | null>(null);
  const [selection, setSelection] = useState<{ conversationId: string; messageId: string; chunkId: string; startMs: number; endMs: number } | null>(null);
  const deleteWarningId = useId();
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const conversationPickerRef = useRef<HTMLDetailsElement>(null);
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
  const { conversationId, messages, progress, loading, searching, error, clear: clearConversation, send } = useAgentConversation(requestedConversationId);
  const isSearchable = (status: string) => status === "READY";
  const scopeValid = selectedSeriesIds.every((id) => series.some((item) => item.seriesId === id && isSearchable(item.status)));
  const canSend = Boolean(query.trim() && !loading && !searching && conversationId && !deletedConversationIdsRef.current.has(conversationId) && scopeValid && series.some((item) => isSearchable(item.status)) && mode === "live");
  const evidenceMessage = selection?.conversationId === conversationId ? messages.find((message) => message.messageId === selection.messageId) : null;
  const selectedHit = evidenceMessage?.searchResult?.hits.find((hit) => hit.evidence.videoChunkId === selection?.chunkId) ?? null;
  const referenceMessage = reference ? messages.find((message) => message.messageId === reference.messageId) : null;
  const referenceHit = referenceMessage?.searchResult?.hits.find((hit) => hit.evidence.videoChunkId === reference?.videoChunkId) ?? null;
  const referenceBlock = referenceMessage && reference ? messageRenderBlocks(referenceMessage).find((block) => block.type === "video" && block.videoChunkId === reference.videoChunkId) : null;
  const referenceRange = referenceHit && referenceBlock?.type === "video" ? videoPlaybackRange(referenceBlock, referenceHit) : null;
  const activeConversation = conversations.find((item) => item.conversationId === conversationId) ?? null;

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
    followThread.current = true;
    setConversationStatus("问题已发送，正在检索");
    void send(query, selectedSeriesIds, reference);
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

  return (
    <main className={`${styles.page} page`}>
      <header className={styles.pageHeader}>
        <div className={styles.chatHeading}>
          <h1 className={styles.chatTitle}>名场面检索助手</h1>
          {activeConversation ? <span title={activeConversation.title}>{activeConversation.title}</span> : null}
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

      <div className={selectedHit ? `${styles.workspace} ${styles.withEvidence}` : styles.workspace}>
        <section className={styles.agentPanel} aria-label="与名场面检索助手对话">
          <div className={styles.thread} ref={threadRef} onScroll={(event) => {
            const thread = event.currentTarget;
            followThread.current = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
          }}>
            {!messages.length && !loading ? (
              <div className={styles.welcome}>
                <ScanSearch size={24} strokeWidth={1.4} aria-hidden="true" />
                <p>描述你记得的对白、人物或动作。</p>
              </div>
            ) : null}
            {messages.map((message) => (
              <article key={message.messageId} className={message.role === "USER" ? styles.userMessage : styles.agentMessage}>
                {message.role === "USER" ? (
                  <div>
                    <p>{message.content}</p>
                    {message.seriesIds.length ? <small className={styles.messageScope}>检索范围：{message.seriesIds.map((id) => series.find((item) => item.seriesId === id)?.name ?? id).join("、")}</small> : null}
                  </div>
                ) : (
                  <div>
                    <AgentProgress message={message} steps={progress[message.messageId] ?? []} />
                    {message.status === "FAILED" ? <p className={styles.messageError} role="alert">{message.lastError?.message || "本次回答未完成"}</p> : null}
                    {!message.content && message.status === "COMPLETED" && !message.searchResult ? <p className={styles.messageError}>未生成回答</p> : null}
                    {message.searchResult?.warnings.length ? <div className={styles.resultWarnings} role="status">{message.searchResult.warnings.map((warning) => <p key={warning}>{warning}</p>)}</div> : null}
                    {messageRenderBlocks(message).map((block, index, blocks) => {
                      if (block.type === "markdown") return <MarkdownAnswer key={`markdown:${index}`} markdown={block.markdown} />;
                      const hit = message.searchResult?.hits.find((item) => item.evidence.videoChunkId === block.videoChunkId);
                      if (!hit) return null;
                      const explanation = explainHit(hit);
                      const playbackRange = videoPlaybackRange(block, hit);
                      const selected = selection?.messageId === message.messageId && selectedHit?.evidence.videoChunkId === hit.evidence.videoChunkId;
                      const thumbnailUrl = hit.evidence.thumbnail ? mediaUrl(hit.evidence.thumbnail.url) : null;
                      const firstVideo = !blocks.slice(0, index).some((item) => item.type === "video" && message.searchResult?.hits.some((candidate) => candidate.evidence.videoChunkId === item.videoChunkId));
                      return (
                        <section key={`video:${block.videoChunkId}:${playbackRange.startMs}:${playbackRange.endMs}:${index}`} className={`${styles.candidatesPanel} ${styles.renderVideoBlock}`} aria-label="检索助手提交的视频片段">
                          {firstVideo ? <div className={styles.candidateHeading}><h2>视频片段</h2><span>{message.searchResult?.hits.length ?? 0} 段</span></div> : null}
                          <div className={styles.candidateList}>
                            <button
                              type="button"
                              className={selected ? styles.candidateSelected : styles.candidate}
                              onClick={() => setSelection({ conversationId: message.conversationId, messageId: message.messageId, chunkId: hit.evidence.videoChunkId, ...playbackRange })}
                              aria-pressed={selected}
                            >
                              <div className={styles.candidateTop}>
                                <span className={styles.rank}>{hit.candidate.rank}</span>
                                <span>{hit.evidence.seriesName} · {episodeLabel(hit.evidence.episodeOrder)}</span>
                                <time>{formatTimeRange(playbackRange.startMs, playbackRange.endMs)}</time>
                              </div>
                              {thumbnailUrl ? (
                                <img className={styles.candidateThumbnail} src={thumbnailUrl} alt={hit.evidence.thumbnail?.alt || `${hit.evidence.seriesName} 视频片段首帧`} loading="lazy" />
                              ) : (
                                <span className={styles.candidateThumbnailFallback} role="img" aria-label="该片段暂无首帧缩略图">
                                  <Play size={22} fill="currentColor" aria-hidden="true" />
                                  <span>{hit.evidence.playbackStatus === "READY" ? "暂无首帧 · 点击加载视频" : "暂无可用预览"}</span>
                                </span>
                              )}
                              <strong>{clipText(hit.evidence.videoCompact || hit.evidence.windowSummary, 78)}</strong>
                              <blockquote>{clipText(readableTranscript(hit), 66)}</blockquote>
                              <div className={styles.whyLine}><MessageCircleMore size={14} aria-hidden="true" /><span>{explanation.summary}</span></div>
                            </button>
                          </div>
                        </section>
                      );
                    })}
                    {message.searchResult && !message.searchResult.hits.length ? (
                      <p className={styles.candidateEmpty}>
                        {message.searchResult.retrievalTrace?.some((entry) => entry.resultCount > 0)
                          ? "检索到了候选片段，但本次回答未附带可播放视频，请重试。"
                          : "没有检索到可展示的视频片段，试试换个描述。"}
                      </p>
                    ) : null}
                  </div>
                )}
              </article>
            ))}
            {loading || (searching && !messages.some((message) => message.status === "PROCESSING")) ? <p className={styles.connectionState} role="status"><LoaderCircle size={15} className={styles.spinning} aria-hidden="true" />{loading ? "正在恢复会话…" : "正在连接检索助手…"}</p> : null}
            {error && !messages.some((message) => message.status === "FAILED" && message.lastError?.message === error) ? <p ref={conversationFailureRef} className={styles.messageError} role="alert" tabIndex={-1}>{error}</p> : null}
          </div>

          <div className={styles.composerArea}>
            <form className={styles.composer} onSubmit={submit}>
              {reference ? <div className={styles.referenceChip}><span>已引用{referenceHit ? ` ${referenceHit.evidence.seriesName} · ${episodeLabel(referenceHit.evidence.episodeOrder)} · ${formatTimeRange(referenceRange?.startMs ?? referenceHit.evidence.startMs, referenceRange?.endMs ?? referenceHit.evidence.endMs)}` : "选中片段"}</span><button type="button" onClick={() => setReference(null)} aria-label="取消引用"><X size={13} /></button></div> : null}
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
                placeholder="描述你记得的对白、人物或动作…"
                rows={2}
                aria-label="向名场面检索助手提问"
              />
              <button className={styles.sendButton} type="submit" disabled={!canSend} aria-label="发送问题">
                {searching ? <LoaderCircle size={18} className={styles.spinning} aria-hidden="true" /> : <ArrowUp size={19} aria-hidden="true" />}
              </button>
            </form>
            <div className={styles.composerToolbar}>
              <details className={styles.scopePicker}>
                <summary aria-label="选择检索视频系列">{selectedSeriesIds.length ? `已选 ${selectedSeriesIds.length} 个系列` : "全部视频系列"}<ChevronDown size={13} aria-hidden="true" /></summary>
                <div className={styles.scopeOptions} role="group" aria-label="检索视频系列多选">
                  <button type="button" onClick={() => setSelectedSeriesIds([])}>检索全部视频系列</button>
                  {series.map((item) => (
                    <label key={item.seriesId}>
                      <input
                        type="checkbox"
                        checked={selectedSeriesIds.includes(item.seriesId)}
                        disabled={!isSearchable(item.status) && !selectedSeriesIds.includes(item.seriesId)}
                        onChange={(event) => setSelectedSeriesIds((current) => event.target.checked ? [...current, item.seriesId] : current.filter((id) => id !== item.seriesId))}
                      />
                      <span>{item.name}</span>
                      <small>{isSearchable(item.status) ? `${item.readyVideoCount} 个视频可检索` : "暂不可检索"}</small>
                    </label>
                  ))}
                  {!series.length ? <p>资料库还没有视频系列</p> : null}
                </div>
              </details>
              <span className={styles.keyboardHint}>Enter 发送 · Shift + Enter 换行</span>
            </div>
            {!scopeValid ? <p className={styles.messageError} role="alert">所选视频系列暂不可检索，请重新选择范围。</p> : null}
          </div>
        </section>

        {selectedHit && evidenceMessage && selection ? (
          <section className={styles.evidenceColumn} aria-label="片段详情" onKeyDown={(event) => {
            if (event.key === "Escape") setSelection(null);
          }}>
            <header className={styles.evidenceHeader}>
              <strong>片段详情</strong>
              <button type="button" onClick={() => setSelection(null)} aria-label="关闭片段详情"><X size={18} /></button>
            </header>
            <EvidencePlayer hit={selectedHit} playbackRange={{ startMs: selection.startMs, endMs: selection.endMs }} />
            <button className={`button-secondary ${styles.referenceButton}`} type="button" onClick={() => {
              setReference({ messageId: evidenceMessage.messageId, videoChunkId: selectedHit.evidence.videoChunkId });
              setSelection(null);
              composerRef.current?.focus();
            }}>引用此片段追问</button>
          </section>
        ) : null}
      </div>
    </main>
  );
}
