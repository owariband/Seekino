import { ChevronDown, ChevronUp, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type MouseEvent, type RefObject } from "react";

import { errorMessage, getVideoTextTimeline } from "../../api/client";
import { useI18n } from "../../app/i18n";
import { formatTimecode } from "../../shared/format";
import { useEpisodeVisualFind } from "./useEpisodeVisualFind";
import { useWatchFindQuery } from "./useWatchFindQuery";
import { useWatchPreferences } from "./watchPreferences";
import type { createVideoTextSearch } from "./videoTextSearch";
import styles from "./VideoTextFind.module.css";

interface Props {
  videoId: string;
  tenantId: string;
  inputRef: RefObject<HTMLInputElement | null>;
  canSeek: boolean;
  onSeek: (positionMs: number) => void;
  onClose: () => void;
}

type TextIndex = {
  ready: boolean;
  search: ReturnType<typeof createVideoTextSearch>;
};

function matchKey(match: { startMs: number; endMs: number; source: string }) {
  return [match.startMs, match.endMs, match.source].join("/");
}

export function VideoTextFind({ videoId, tenantId, inputRef, canSeek, onSeek, onClose }: Props) {
  const { t, locale } = useI18n();
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState<TextIndex | null>(null);
  const [failure, setFailure] = useState<Error | null>(null);
  const [attempt, setAttempt] = useState(0);
  const { visualFind, setVisualFind } = useWatchPreferences();
  const [smartFind, setSmartFind] = useState(false);
  const [rewriteAttempt, setRewriteAttempt] = useState(0);
  const smart = useWatchFindQuery(videoId, tenantId, query, smartFind, rewriteAttempt);
  const visual = useEpisodeVisualFind(videoId, tenantId, smart.terms, smart.semanticQuery, visualFind, attempt);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const selectedRef = useRef<HTMLButtonElement>(null);
  const resultsRef = useRef<HTMLOListElement>(null);

  useEffect(() => { inputRef.current?.focus(); }, [inputRef]);
  useEffect(() => {
    const controller = new AbortController();
    setIndex(null);
    setFailure(null);
    setActiveKey(null);
    Promise.all([getVideoTextTimeline(videoId, tenantId, controller.signal), import("./videoTextSearch")])
      .then(([timeline, { createVideoTextSearch }]) => {
        if (controller.signal.aborted) return;
        setIndex({ ready: timeline.ready, search: createVideoTextSearch(timeline.cues) });
      })
      .catch((error: Error) => { if (!controller.signal.aborted) setFailure(error); });
    return () => controller.abort();
  }, [tenantId, videoId, attempt]);

  const matches = useMemo(() => {
    const literal = (index?.search(smart.terms) ?? []).map(({ cue, text }) => ({
      startMs: cue.startMs, endMs: cue.endMs, source: cue.source, text,
    }));
    return [...literal, ...visual.matches].map(match => ({
      ...match,
      // A shot can begin during the preceding cut. Preview its interior; keep utterances at their onset.
      positionMs: match.source === "visual" || match.source === "related"
        ? match.startMs + Math.floor((match.endMs - match.startMs) / 2) : match.startMs,
    })).sort((a, b) => a.positionMs - b.positionMs);
  }, [index, smart.terms, visual.matches]);
  const active = matches.findIndex(match => matchKey(match) === activeKey);
  const sourceLabels = { dialogue: t("台词"), screen_text: t("画面文字"), visual: t("画面"), related: t("相关画面") };
  const visualStatus = visual.timelineUnavailable ? t("本集画面标注尚未就绪")
    : visual.semanticUnavailable ? t("语义检索暂不可用") : visual.semanticPartial ? t("部分画面索引暂不可用") : null;
  const label = visualFind ? t("查找文字或画面") : t("查找台词或画面文字");

  useEffect(() => {
    if (resultsRef.current?.contains(document.activeElement)) selectedRef.current?.focus({ preventScroll: true });
    selectedRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [active]);

  function select(position: number) {
    if (!canSeek || !matches.length) return;
    const next = (position + matches.length) % matches.length;
    const match = matches[next]!;
    setActiveKey(matchKey(match));
    onSeek(match.positionMs);
  }

  const previous = () => select(active <= 0 ? matches.length - 1 : active - 1);
  const next = () => select(active + 1);
  const disabled = !canSeek || matches.length === 0;

  function keepTypingFocus(event: MouseEvent<HTMLButtonElement>) {
    if (event.button === 0 && document.activeElement === inputRef.current) event.preventDefault();
  }

  return <section className={styles.textFind} role="search" aria-label={t("在本视频中查找")}
    onKeyDown={event => {
      if (event.nativeEvent.isComposing || event.keyCode === 229) return;
      if (event.key === "Escape") {
        event.preventDefault(); event.stopPropagation(); onClose();
      } else if (event.target === inputRef.current && event.key === "Enter") {
        event.preventDefault(); event.shiftKey ? previous() : next();
      } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault(); event.key === "ArrowDown" ? next() : previous();
      }
    }}>
    <div className={styles.textFindBar}>
      <input ref={inputRef} value={query} onChange={event => { setQuery(event.target.value); setActiveKey(null); }}
        aria-label={label} placeholder={label} />
      {(index?.ready || matches.length > 0) && query.trim() ? <output aria-live="polite">{active < 0 ? matches.length : `${active + 1}/${matches.length}`}</output> : null}
      <button type="button" disabled={disabled} aria-label={t("上一处")} onClick={previous}><ChevronUp size={16} /></button>
      <button type="button" disabled={disabled} aria-label={t("下一处")} onClick={next}><ChevronDown size={16} /></button>
      <button type="button" aria-label={t("关闭片内查找")} onClick={onClose}><X size={16} /></button>
    </div>
    <div className={styles.findFilters}>
      <button type="button" role="switch" aria-checked={visualFind} aria-label={t("画面搜索")} onMouseDown={keepTypingFocus}
        onClick={event => {
          setVisualFind(!visualFind);
          if (event.detail > 0) inputRef.current?.focus();
        }}>
        <i aria-hidden="true" />{t("画面")}
      </button>
      <button type="button" role="switch" aria-checked={smartFind} aria-label={t("智能搜索")} onMouseDown={keepTypingFocus}
        onClick={event => {
          setSmartFind(!smartFind);
          setActiveKey(null);
          if (event.detail > 0) inputRef.current?.focus();
        }}>
        <i aria-hidden="true" />{t("智能搜索")}
      </button>
    </div>
    {smart.loading ? <p className={styles.textFindStatus} role="status">{t("正在理解搜索词…")}</p>
      : smart.failed ? <div className={styles.textFindStatus} role="status">
        {t("智能搜索暂不可用，已按原词查找")}
        <button type="button" onClick={() => setRewriteAttempt(value => value + 1)}>{t("重试")}</button>
      </div> : smart.terms.length > 1 ? <p className={styles.findTerms} role="status">
        {t("纠正")} <strong>{smart.terms.slice(1).join(locale === "zh" ? "、" : ", ")}</strong>
      </p> : null}
    {failure || index?.ready === false ? <div className={styles.textFindStatus} role="status">
      {failure ? errorMessage(failure, locale) : t("本集文字尚未就绪")}
      <button type="button" onClick={() => setAttempt(value => value + 1)}>{t("重试")}</button>
    </div> : !index ? <p className={styles.textFindStatus} role="status">{t("正在读取本集文字…")}</p>
      : null}
    {visualStatus ? <div className={styles.textFindStatus} role="status">
      {visualStatus}
      <button type="button" onClick={() => setAttempt(value => value + 1)}>{t("重试")}</button>
    </div> : query.trim() && !matches.length && index?.ready && !smart.loading ? <p className={styles.textFindStatus} role="status">
      {visual.loading ? t("正在查找画面…") : visualFind ? t("没有匹配的结果") : t("没有匹配的文字")}
    </p> : null}
    {matches.length > 0 && <ol className={styles.textFindResults} ref={resultsRef}>
      {matches.map((match, i) => <li key={matchKey(match)}>
        <button type="button" aria-current={active === i ? "true" : undefined} disabled={!canSeek}
          ref={active === i ? selectedRef : undefined} onClick={() => select(i)}
          onKeyDown={event => {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            select(i + (event.key === "ArrowRight" ? 1 : -1));
          }}>
          <time title={`${formatTimecode(match.startMs)}–${formatTimecode(match.endMs)}`}>{formatTimecode(match.positionMs)}</time>
          <small>{sourceLabels[match.source]}</small>
          <span title={match.text}>{match.text}</span>
        </button>
      </li>)}
    </ol>}
  </section>;
}
