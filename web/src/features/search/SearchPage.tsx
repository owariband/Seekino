import { ArrowUp, Bot, Check, MessageCircleMore, Sparkles } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { useWorkspace } from "../../app/workspace";
import type { EvidenceReference } from "../../api/contracts";
import { clipText, episodeLabel, formatTimeRange } from "../../shared/format";
import { EvidencePlayer } from "./EvidencePlayer";
import { explainHit, readableTranscript } from "./explain";
import { useAgentConversation } from "./useAgentConversation";
import styles from "./search.module.css";

export function SearchPage() {
  const { mode, series } = useWorkspace();
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [reference, setReference] = useState<EvidenceReference | null>(null);
  const [selection, setSelection] = useState<{ conversationId: string; messageId: string; chunkId: string | null } | null>(null);
  const threadEnd = useRef<HTMLDivElement>(null);
  const { conversationId, messages, loading, searching, error, send } = useAgentConversation();
  const isSearchable = (status: string) => status === "READY" || status === "PARTIAL";
  const scopeValid = selectedSeriesIds.every((id) => series.some((item) => item.seriesId === id && isSearchable(item.status)));
  const canSend = Boolean(query.trim() && !loading && !searching && conversationId && scopeValid && series.some((item) => isSearchable(item.status)) && mode === "live");
  const latestEvidence = messages.toReversed().find((message) => message.searchResult);
  const evidenceMessage = selection?.conversationId === conversationId
    ? messages.find((message) => message.messageId === selection.messageId) ?? latestEvidence
    : latestEvidence;
  const result = evidenceMessage?.searchResult ?? null;
  const referenceHit = reference ? messages.find((message) => message.messageId === reference.messageId)?.searchResult?.hits.find((hit) => hit.evidence.videoChunkId === reference.videoChunkId) : null;
  const selectedHit = (selection?.conversationId === conversationId
    ? result?.hits.find((hit) => hit.evidence.videoChunkId === selection.chunkId)
    : null) ?? result?.hits[0] ?? null;

  useEffect(() => { setSelection(null); setReference(null); }, [conversationId]);

  useEffect(() => {
    threadEnd.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  }, [messages, searching]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSend) return;
    void send(query, selectedSeriesIds, reference);
    setQuery("");
    setReference(null);
  };

  return (
    <main className={`${styles.page} page`}>
      <header className={styles.pageHeader}>
        <div>
          <p className="page-kicker">Scene inquiry · 画面问询</p>
          <h1 className="page-title">问记忆，找画面。</h1>
        </div>
        <div className={styles.scope}>
          <span className="eyebrow">当前检索范围</span>
          <details className={styles.scopePicker}>
            <summary aria-label="选择检索动画">{selectedSeriesIds.length ? `已选 ${selectedSeriesIds.length} 部动画` : "全部动画"}</summary>
            <div className={styles.scopeOptions} role="group" aria-label="检索动画多选">
              <button type="button" onClick={() => setSelectedSeriesIds([])}>检索全部动画</button>
              {series.map((item) => (
                <label key={item.seriesId}>
                  <input
                    type="checkbox"
                    checked={selectedSeriesIds.includes(item.seriesId)}
                    disabled={!isSearchable(item.status) && !selectedSeriesIds.includes(item.seriesId)}
                    onChange={(event) => setSelectedSeriesIds((current) => event.target.checked ? [...current, item.seriesId] : current.filter((id) => id !== item.seriesId))}
                  />
                  <span>{item.name}</span>
                  <small>{isSearchable(item.status) ? `${item.readyVideoCount} 集可检索` : "暂不可检索"}</small>
                </label>
              ))}
              {!series.length ? <p>资料库还没有动画</p> : null}
            </div>
          </details>
          <small>可多选 · 未选择时检索全部可检索动画</small>
          {!scopeValid ? <small className={styles.messageError} role="alert">所选动画暂不可检索，请重新选择范围。</small> : null}
        </div>
      </header>

      <div className={styles.workspace}>
        <section className={styles.agentPanel} aria-label="与检索 Agent 对话">
          <div className={styles.panelHeading}>
            <div className={styles.agentSeal}><Sparkles size={16} /></div>
            <div>
              <strong>画面札记 Agent</strong>
              <span><i /> {mode === "live" ? "API 已连接" : mode === "checking" ? "正在连接" : "API 不可用"}</span>
            </div>
          </div>
          <div className={styles.thread} aria-live="polite">
            {!messages.length ? (
              <article className={styles.agentMessage}>
                <Bot size={14} />
                <div><span>AGENT</span><p>告诉我你记得的人物、对白、动作或氛围。我会召回几段备选画面，并说明每一段为什么值得核验。</p></div>
              </article>
            ) : null}
            {messages.map((message) => (
              <article key={message.messageId} className={message.role === "USER" ? styles.userMessage : styles.agentMessage}>
                {message.role === "ASSISTANT" ? <Bot size={14} /> : null}
                <div>
                  <span>{message.role === "ASSISTANT" ? "AGENT" : "YOU"}</span>
                  {message.role === "USER" ? <small className={styles.messageScope}>检索范围：{message.seriesIds.map((id) => series.find((item) => item.seriesId === id)?.name ?? id).join("、") || "无可检索动画"}</small> : null}
                  <p className={message.status === "FAILED" ? styles.messageError : !message.content && message.status === "PROCESSING" ? styles.thinking : undefined} style={{ whiteSpace: "pre-wrap" }}>
                    {message.content || (message.status === "PROCESSING" ? ({ QUEUED: "等待执行", RETRIEVING: "正在检索视频证据", EVALUATING: "正在审核召回证据", RESPONDING: "正在组织最终回答" }[message.phase ?? "RETRIEVING"]) : message.lastError?.message || "未生成回答")}
                  </p>
                  {message.status === "FAILED" && message.content ? <p className={styles.messageError}>{message.lastError?.message || "本次回答未完成"}</p> : null}
                  {message.searchResult?.hits.length ? (
                    <button type="button" className="button-secondary" onClick={() => setSelection({ conversationId: message.conversationId, messageId: message.messageId, chunkId: null })}>
                      <Check size={12} /> 查看 {message.searchResult.hits.length} 份视频证据
                    </button>
                  ) : null}
                </div>
              </article>
            ))}
            {loading || (searching && !messages.some((message) => message.status === "PROCESSING")) ? (
              <article className={styles.agentMessage}>
                <Bot size={14} />
                <div><span>AGENT</span><p className={styles.thinking}>{loading ? "正在恢复会话" : "正在准备本次问询"}</p></div>
              </article>
            ) : null}
            {error ? <article className={styles.agentMessage}><Bot size={14} /><div><span>AGENT</span><p className={styles.messageError}>{error}</p></div></article> : null}
            <div ref={threadEnd} />
          </div>
          <form className={styles.composer} onSubmit={submit}>
            {reference ? <p className={styles.messageScope}>已引用{referenceHit ? ` ${referenceHit.evidence.seriesName} · ${episodeLabel(referenceHit.evidence.episodeOrder)} · ${formatTimeRange(referenceHit.evidence.startMs, referenceHit.evidence.endMs)}` : "选中片段"}<button type="button" onClick={() => setReference(null)}>取消引用</button></p> : null}
            <textarea
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder="描述你记得的对白、人物或氛围…"
              rows={3}
              aria-label="向画面札记 Agent 提问"
            />
            <div><span>Enter 发送 · Shift + Enter 换行</span><button type="submit" disabled={!canSend} aria-label="发送问题"><ArrowUp size={17} /></button></div>
          </form>
        </section>

        <section className={styles.candidatesPanel} aria-label="Agent 召回的备选画面">
          <div className={styles.candidateHeading}>
            <div><span className="eyebrow">Recalled scenes</span><h2>备选画面</h2></div>
            <span>{result ? `${result.hits.length} 段` : "等待问询"}</span>
          </div>
          {result?.warnings.length ? <div className={styles.messageError} role="status">{result.partial ? <strong>结果包含资料限制</strong> : null}{result.warnings.map((warning) => <p key={warning}>{warning}</p>)}</div> : null}
          {result?.hits.length ? (
            <div className={styles.candidateList}>
              {result.hits.map((hit) => {
                const explanation = explainHit(hit);
                const selected = selectedHit?.evidence.videoChunkId === hit.evidence.videoChunkId;
                return (
                  <button
                    type="button"
                    key={hit.evidence.videoChunkId}
                    className={selected ? styles.candidateSelected : styles.candidate}
                    onClick={() => { if (conversationId && evidenceMessage) setSelection({ conversationId, messageId: evidenceMessage.messageId, chunkId: hit.evidence.videoChunkId }); }}
                  >
                    <div className={styles.candidateTop}>
                      <span className={styles.rank}>{String(hit.candidate.rank).padStart(2, "0")}</span>
                      <span>{hit.evidence.seriesName} · {episodeLabel(hit.evidence.episodeOrder)}</span>
                      <time>{formatTimeRange(hit.evidence.startMs, hit.evidence.endMs)}</time>
                    </div>
                    <strong>{clipText(hit.evidence.videoCompact || hit.evidence.windowSummary, 78)}</strong>
                    <blockquote>{clipText(readableTranscript(hit), 66)}</blockquote>
                    <div className={styles.whyLine}><MessageCircleMore size={13} /><span>{clipText(explanation.summary, 82)}</span></div>
                    <div className={styles.channelTags}>
                      {explanation.sources.slice(0, 2).map((source) => <span key={source}>{source}</span>)}
                    </div>
                  </button>
                );
              })}
            </div>
          ) : (
            <div className={styles.candidateEmpty}>
              <MessageCircleMore size={22} strokeWidth={1.4} />
              <p>候选不会只给缩略图。每次召回都会附带“为何选择”和可核验证据。</p>
            </div>
          )}
        </section>

        <div>{selectedHit && evidenceMessage ? <button className="button-secondary" type="button" onClick={() => setReference({ messageId: evidenceMessage.messageId, videoChunkId: selectedHit.evidence.videoChunkId })}>引用此片段追问</button> : null}<EvidencePlayer hit={selectedHit} /></div>
      </div>
    </main>
  );
}
