import { ChevronDown, ChevronUp, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";

import { errorMessage, getVideoTextTimeline } from "../../api/client";
import { useI18n } from "../../app/i18n";
import { formatTimecode } from "../../shared/format";
import type { createVideoTextSearch } from "./videoTextSearch";
import styles from "./watch.module.css";

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

export function VideoTextFind({ videoId, tenantId, inputRef, canSeek, onSeek, onClose }: Props) {
  const { t, locale } = useI18n();
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState<TextIndex | null>(null);
  const [failure, setFailure] = useState<Error | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [active, setActive] = useState(-1);
  const selectedRef = useRef<HTMLButtonElement>(null);
  const resultsRef = useRef<HTMLOListElement>(null);

  useEffect(() => { inputRef.current?.focus(); }, [inputRef]);
  useEffect(() => {
    const controller = new AbortController();
    setIndex(null);
    setFailure(null);
    setActive(-1);
    Promise.all([getVideoTextTimeline(videoId, tenantId, controller.signal), import("./videoTextSearch")])
      .then(([timeline, { createVideoTextSearch }]) => {
        if (controller.signal.aborted) return;
        setIndex({ ready: timeline.ready, search: createVideoTextSearch(timeline.cues) });
      })
      .catch((error: Error) => { if (!controller.signal.aborted) setFailure(error); });
    return () => controller.abort();
  }, [tenantId, videoId, attempt]);

  const matches = useMemo(() => index?.search(query) ?? [], [index, query]);

  useEffect(() => {
    if (resultsRef.current?.contains(document.activeElement)) selectedRef.current?.focus({ preventScroll: true });
    selectedRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [active]);

  function select(position: number) {
    if (!canSeek || !matches.length) return;
    const next = (position + matches.length) % matches.length;
    setActive(next);
    onSeek(matches[next]!.cue.startMs);
  }

  const previous = () => select(active <= 0 ? matches.length - 1 : active - 1);
  const next = () => select(active + 1);
  const disabled = !canSeek || matches.length === 0;

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
      <input ref={inputRef} value={query} onChange={event => { setQuery(event.target.value); setActive(-1); }}
        aria-label={t("查找台词或画面文字")} placeholder={t("查找台词或画面文字")} />
      {index?.ready && query.trim() ? <output aria-live="polite">{active < 0 ? matches.length : `${active + 1}/${matches.length}`}</output> : null}
      <button type="button" disabled={disabled} aria-label={t("上一处")} onClick={previous}><ChevronUp size={16} /></button>
      <button type="button" disabled={disabled} aria-label={t("下一处")} onClick={next}><ChevronDown size={16} /></button>
      <button type="button" aria-label={t("关闭片内查找")} onClick={onClose}><X size={16} /></button>
    </div>
    {failure || index?.ready === false ? <div className={styles.textFindStatus} role="status">
      {failure ? errorMessage(failure, locale) : t("本集文字尚未就绪")}
      <button type="button" onClick={() => setAttempt(value => value + 1)}>{t("重试")}</button>
    </div> : !index ? <p className={styles.textFindStatus} role="status">{t("正在读取本集文字…")}</p>
      : query.trim() && !matches.length ? <p className={styles.textFindStatus} role="status">{t("没有匹配的文字")}</p> : null}
    {matches.length > 0 && <ol className={styles.textFindResults} ref={resultsRef}>
      {matches.map(({ cue, text }, i) => <li key={`${cue.startMs}-${cue.endMs}-${cue.source}`}>
        <button type="button" aria-current={active === i ? "true" : undefined} disabled={!canSeek}
          ref={active === i ? selectedRef : undefined} onClick={() => select(i)}
          onKeyDown={event => {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            select(i + (event.key === "ArrowRight" ? 1 : -1));
          }}>
          <time>{formatTimecode(cue.startMs)}</time>
          <small>{cue.source === "dialogue" ? t("台词") : t("画面文字")}</small><span>{text}</span>
        </button>
      </li>)}
    </ol>}
  </section>;
}
