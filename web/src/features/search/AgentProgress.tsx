import { ChevronDown } from "lucide-react";
import { useEffect, useState } from "react";

import type { AgentMessageTiming, AgentMessageView, AnswerPhase, DisplayMode } from "../../api/contracts";
import { formatTimecode as formatDuration } from "../../shared/format";
import type { KinoReaction } from "../../shared/kino/Kino";
import { ProgressMascot } from "./ProgressMascot";
import type { AgentProgressEvent } from "./useAgentConversation";
import { useI18n } from "../../app/i18n";
import styles from "./search.module.css";

const phases: Record<AnswerPhase, { label: string; detail: string }> = {
  QUEUED: { label: "等待处理", detail: "服务端已接收问题。" },
  RETRIEVING: { label: "查找资料", detail: "查找回答问题所需的资料。" },
  EVALUATING: { label: "核验资料", detail: "检查资料是否支持回答。" },
  RESPONDING: { label: "生成回答", detail: "整理回答和已查到的资料。" },
};

type VisibleStage = {
  phase: AnswerPhase;
  durationMs: number | null;
  key: string;
};

function latestTiming(message: AgentMessageView, steps: AgentProgressEvent[]): AgentMessageTiming | null {
  return message.timing ?? [...steps].reverse().find((step) => step.timing)?.timing ?? null;
}

export function AgentProgress({ message, steps, displayMode = "release", kinoHost = true, kinoAwake = false }: {
  message: AgentMessageView;
  steps: AgentProgressEvent[];
  displayMode?: DisplayMode;
  /** Kino perches on the latest assistant bubble only; it acts out the answer here, then sleeps on it. */
  kinoHost?: boolean;
  /** A new question is on its way: wake a sleeping Kino before it leaps to the next bubble. */
  kinoAwake?: boolean;
}) {
  const { t } = useI18n();
  const processing = message.status === "PROCESSING";
  const failed = message.status === "FAILED";
  // When a live answer settles Kino cheers (or startles on failure), then falls asleep on this bubble.
  const [wasProcessing, setWasProcessing] = useState(processing);
  const [farewell, setFarewell] = useState<KinoReaction | null>(null);
  if (wasProcessing !== processing) {
    setWasProcessing(processing);
    setFarewell(processing ? null : failed ? "oops" : "happy");
  }
  const timing = latestTiming(message, steps);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!processing || !timing) return undefined;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [processing, timing?.startedAt]);
  const elapsedMs = timing?.totalDurationMs ?? (
    processing && timing ? Math.max(0, now - timing.startedAt) : null
  );
  const timedStages = timing?.stages.map((stage, index) => ({
    phase: stage.phase,
    durationMs: stage.durationMs,
    key: `${index}:${stage.phase}`,
  })) ?? [];
  const observedSteps = processing && message.phase && steps.at(-1)?.phase !== message.phase
    ? [...steps, { type: "answer.progress" as const, conversationId: message.conversationId, messageId: message.messageId, sequence: 0, occurredAt: message.updatedAt, phase: message.phase }]
    : steps;
  const visibleSteps: VisibleStage[] = timing ? [...timedStages] : observedSteps.map((step, index) => ({
    phase: step.phase,
    durationMs: null,
    key: `${step.sequence}:${index}:${step.phase}`,
  }));
  if (processing && message.phase && timing) {
    visibleSteps.push({
      phase: message.phase,
      durationMs: Math.max(0, (elapsedMs ?? 0) - timedStages.reduce((sum, stage) => sum + stage.durationMs, 0)),
      key: `active:${timedStages.length}:${message.phase}`,
    });
  }
  const currentPhase = message.phase ?? visibleSteps.at(-1)?.phase ?? steps.at(-1)?.phase;
  const kino = kinoHost
    ? <ProgressMascot phase={processing ? currentPhase : null} settled={!processing} awake={kinoAwake} arriving={processing}
      farewell={farewell} onFarewellDone={() => setFarewell(null)} />
    : null;
  const status = failed ? t("回答未完成") : processing ? displayMode === "debug" && currentPhase ? t(phases[currentPhase].label) : t("处理中") : t("已完成");
  const timingStatus = elapsedMs === null
    ? status
    : `${status} · ${processing ? t("已用") : t("总耗时")} ${formatDuration(elapsedMs)}`;

  return <>
    {kino}
    <div className={styles.progressCard}>
      <div role="status" aria-label={t("回答状态")}><p className={failed ? styles.messageError : styles.progressNote}>{timingStatus}</p></div>
      {displayMode === "debug" ? <details key={message.status} className={styles.progressDetails}>
        <summary aria-label={t("回答过程")}>
          <span>{t("回答过程")}</span>
          <ChevronDown size={14} className={styles.progressChevron} aria-hidden="true" />
        </summary>
        {visibleSteps.length ? (
          <ol className={styles.progressSteps} aria-label={t("已同步的执行步骤")}>
            {visibleSteps.map((step, index) => {
              const phase = phases[step.phase];
              const latest = index === visibleSteps.length - 1;
              const state = latest && processing ? "active" : latest && failed ? "interrupted" : "done";
              return (
                <li key={step.key} data-state={state}>
                  <div><div className={styles.stepHeading}><strong>{t(phase.label)}</strong><span>{state === "active" ? t("进行中") : state === "interrupted" ? t("未确认完成") : t("已执行")}{step.durationMs === null ? "" : ` · ${formatDuration(step.durationMs)}`}</span></div><p>{t(phase.detail)}</p></div>
                </li>
              );
            })}
          </ol>
        ) : <p className={styles.progressNote}>{processing ? t("正在同步执行状态…") : failed ? t("本次回答未完成。") : t("历史消息仅保留最终状态，完整执行步骤暂不可回放。")}</p>}
        {message.searchResult ? <p className={styles.progressNote}>{t("{videos} 个视频 · {clips} 段候选", { videos: message.searchResult.searchedVideoIds.length, clips: message.searchResult.hits.length })}{message.searchResult.partial ? t(" · 结果有资料限制") : ""}</p> : null}
      </details> : null}
    </div>
  </>;
}
