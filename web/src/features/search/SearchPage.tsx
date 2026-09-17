import { ArrowUp, ChevronDown, LoaderCircle, MessageCircleMore, ScanSearch, X } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";
import Markdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

import { useWorkspace } from "../../app/workspace";
import { mediaUrl } from "../../api/client";
import type { AgentMessageView, AgentRenderBlock, EvidenceReference } from "../../api/contracts";
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
    })) ?? []),
  ];
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
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [reference, setReference] = useState<EvidenceReference | null>(null);
  const [selection, setSelection] = useState<{ conversationId: string; messageId: string; chunkId: string } | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const followThread = useRef(true);
  const { conversationId, messages, progress, loading, searching, error, send } = useAgentConversation();
  const isSearchable = (status: string) => status === "READY";
  const scopeValid = selectedSeriesIds.every((id) => series.some((item) => item.seriesId === id && isSearchable(item.status)));
  const canSend = Boolean(query.trim() && !loading && !searching && conversationId && scopeValid && series.some((item) => isSearchable(item.status)) && mode === "live");
  const evidenceMessage = selection?.conversationId === conversationId ? messages.find((message) => message.messageId === selection.messageId) : null;
  const selectedHit = evidenceMessage?.searchResult?.hits.find((hit) => hit.evidence.videoChunkId === selection?.chunkId) ?? null;
  const referenceHit = reference ? messages.find((message) => message.messageId === reference.messageId)?.searchResult?.hits.find((hit) => hit.evidence.videoChunkId === reference.videoChunkId) : null;

  useEffect(() => { setSelection(null); setReference(null); }, [conversationId]);

  useEffect(() => {
    if (followThread.current && threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight;
  }, [messages, searching]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSend) return;
    followThread.current = true;
    void send(query, selectedSeriesIds, reference);
    setQuery("");
    setReference(null);
  };

  return (
    <main className={`${styles.page} page`}>
      <header className={styles.pageHeader}>
        <h1 className={styles.chatTitle}>名场面检索助手</h1>
      </header>

      <div className={selectedHit ? `${styles.workspace} ${styles.withEvidence}` : styles.workspace}>
        <section className={styles.agentPanel} aria-label="与名场面检索助手对话">
          <div className={styles.thread} ref={threadRef} aria-live="polite" onScroll={(event) => {
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
                      const selected = selection?.messageId === message.messageId && selectedHit?.evidence.videoChunkId === hit.evidence.videoChunkId;
                      const thumbnailUrl = hit.evidence.thumbnail ? mediaUrl(hit.evidence.thumbnail.url) : null;
                      const firstVideo = !blocks.slice(0, index).some((item) => item.type === "video" && message.searchResult?.hits.some((candidate) => candidate.evidence.videoChunkId === item.videoChunkId));
                      return (
                        <section key={`video:${block.videoChunkId}:${index}`} className={`${styles.candidatesPanel} ${styles.renderVideoBlock}`} aria-label="检索助手提交的视频片段">
                          {firstVideo ? <div className={styles.candidateHeading}><h2>视频片段</h2><span>{message.searchResult?.hits.length ?? 0} 段</span></div> : null}
                          <div className={styles.candidateList}>
                            <button
                              type="button"
                              className={selected ? styles.candidateSelected : styles.candidate}
                              onClick={() => setSelection({ conversationId: message.conversationId, messageId: message.messageId, chunkId: hit.evidence.videoChunkId })}
                              aria-pressed={selected}
                            >
                              <div className={styles.candidateTop}>
                                <span className={styles.rank}>{hit.candidate.rank}</span>
                                <span>{hit.evidence.seriesName} · {episodeLabel(hit.evidence.episodeOrder)}</span>
                                <time>{formatTimeRange(hit.evidence.startMs, hit.evidence.endMs)}</time>
                              </div>
                              {thumbnailUrl ? <img className={styles.candidateThumbnail} src={thumbnailUrl} alt={hit.evidence.thumbnail?.alt || `${hit.evidence.seriesName} 视频片段首帧`} loading="lazy" /> : null}
                              <strong>{clipText(hit.evidence.videoCompact || hit.evidence.windowSummary, 78)}</strong>
                              <blockquote>{clipText(readableTranscript(hit), 66)}</blockquote>
                              <div className={styles.whyLine}><MessageCircleMore size={14} aria-hidden="true" /><span>{explanation.summary}</span></div>
                            </button>
                          </div>
                        </section>
                      );
                    })}
                    {message.searchResult && !message.searchResult.hits.length ? <p className={styles.candidateEmpty}>没有可展示的视频片段，试试换个描述。</p> : null}
                  </div>
                )}
              </article>
            ))}
            {loading || (searching && !messages.some((message) => message.status === "PROCESSING")) ? <p className={styles.connectionState} role="status"><LoaderCircle size={15} className={styles.spinning} aria-hidden="true" />{loading ? "正在恢复会话…" : "正在连接检索助手…"}</p> : null}
            {error && !messages.some((message) => message.status === "FAILED" && message.lastError?.message === error) ? <p className={styles.messageError} role="alert">{error}</p> : null}
          </div>

          <div className={styles.composerArea}>
            <form className={styles.composer} onSubmit={submit}>
              {reference ? <div className={styles.referenceChip}><span>已引用{referenceHit ? ` ${referenceHit.evidence.seriesName} · ${episodeLabel(referenceHit.evidence.episodeOrder)} · ${formatTimeRange(referenceHit.evidence.startMs, referenceHit.evidence.endMs)}` : "选中片段"}</span><button type="button" onClick={() => setReference(null)} aria-label="取消引用"><X size={13} /></button></div> : null}
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

        {selectedHit && evidenceMessage ? (
          <section className={styles.evidenceColumn} aria-label="片段详情" onKeyDown={(event) => {
            if (event.key === "Escape") setSelection(null);
          }}>
            <header className={styles.evidenceHeader}>
              <strong>片段详情</strong>
              <button type="button" onClick={() => setSelection(null)} aria-label="关闭片段详情"><X size={18} /></button>
            </header>
            <EvidencePlayer hit={selectedHit} />
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
