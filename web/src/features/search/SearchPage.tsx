import { ArrowUp, Bot, Check, MessageCircleMore, Sparkles } from "lucide-react";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { useWorkspace } from "../../app/workspace";
import { clipText, episodeLabel, formatTimeRange } from "../../shared/format";
import { EvidencePlayer } from "./EvidencePlayer";
import { explainHit, readableTranscript } from "./explain";
import { useAgentConversation } from "./useAgentConversation";
import styles from "./search.module.css";

export function SearchPage() {
  const { mode, series, selectedSeries, selectedSeriesId, selectSeries } = useWorkspace();
  const [query, setQuery] = useState("");
  const [selection, setSelection] = useState<{ conversationId: string; messageId: string; chunkId: string | null } | null>(null);
  const threadEnd = useRef<HTMLDivElement>(null);
  const searchSeriesId = selectedSeries?.status === "READY" ? selectedSeries.seriesId : null;
  const { conversationId, messages, loading, searching, error, send } = useAgentConversation(searchSeriesId);
  const latestEvidence = messages.toReversed().find((message) => message.searchResult);
  const evidenceMessage = selection?.conversationId === conversationId
    ? messages.find((message) => message.messageId === selection.messageId) ?? latestEvidence
    : latestEvidence;
  const result = evidenceMessage?.searchResult ?? null;
  const selectedHit = (selection?.conversationId === conversationId
    ? result?.hits.find((hit) => hit.evidence.videoChunkId === selection.chunkId)
    : null) ?? result?.hits[0] ?? null;

  useEffect(() => { setQuery(""); setSelection(null); }, [searchSeriesId]);

  useEffect(() => {
    threadEnd.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  }, [messages, searching]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim() || !conversationId || searching) return;
    void send(query);
    setQuery("");
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
          <select
            aria-label="选择检索系列"
            value={selectedSeriesId ?? ""}
            onChange={(event) => selectSeries(event.target.value)}
          >
            {series.map((item) => <option key={item.seriesId} value={item.seriesId}>{item.name}</option>)}
          </select>
          <small>{selectedSeries?.videoCount ?? 0} 集 · {selectedSeries?.status ?? "EMPTY"}</small>
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
                  <p className={message.status === "FAILED" ? styles.messageError : !message.content && message.status === "PROCESSING" ? styles.thinking : undefined} style={{ whiteSpace: "pre-wrap" }}>
                    {message.content || (message.status === "PROCESSING" ? "正在比对对白、视觉事件与前后语境" : message.lastError || "未生成回答")}
                  </p>
                  {message.status === "FAILED" && message.content ? <p className={styles.messageError}>{message.lastError || "本次回答未完成"}</p> : null}
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
            <div><span>Enter 发送 · Shift + Enter 换行</span><button type="submit" disabled={!query.trim() || loading || searching || !conversationId || !searchSeriesId || mode !== "live"} aria-label="发送问题"><ArrowUp size={17} /></button></div>
          </form>
        </section>

        <section className={styles.candidatesPanel} aria-label="Agent 召回的备选画面">
          <div className={styles.candidateHeading}>
            <div><span className="eyebrow">Recalled scenes</span><h2>备选画面</h2></div>
            <span>{result ? `${result.hits.length} 段` : "等待问询"}</span>
          </div>
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
                      <span className={styles.rank}>{String(hit.candidate.fusedRank).padStart(2, "0")}</span>
                      <span>{episodeLabel(hit.evidence.episodeOrder)}</span>
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

        <EvidencePlayer hit={selectedHit} />
      </div>
    </main>
  );
}
