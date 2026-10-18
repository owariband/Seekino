import { Check, ChevronDown, Clock3, LoaderCircle, MessageSquareText, ScanSearch, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";

import type { AgentMessageTiming, AgentMessageView, AnswerPhase, DisplayMode } from "../../api/contracts";
import { formatTimecode as formatDuration } from "../../shared/format";
import { Mascot, type MascotMood } from "../../shared/Mascot";
import type { AgentProgressEvent } from "./useAgentConversation";
import { useI18n } from "../../app/i18n";
import styles from "./search.module.css";

const phases: Record<AnswerPhase, { label: string; detail: string; icon: typeof Clock3 }> = {
  QUEUED: { label: "等待处理", detail: "服务端已接收问题。", icon: Clock3 },
  RETRIEVING: { label: "查找资料", detail: "查找回答问题所需的资料。", icon: ScanSearch },
  EVALUATING: { label: "核验资料", detail: "检查资料是否支持回答。", icon: ShieldCheck },
  RESPONDING: { label: "生成回答", detail: "整理回答和已查到的资料。", icon: MessageSquareText },
};

const phaseMoods: Record<AnswerPhase, MascotMood> = {
  QUEUED: "seek",
  RETRIEVING: "seek",
  EVALUATING: "puzzled",
  RESPONDING: "swim",
};

type VisibleStage = {
  phase: AnswerPhase;
  durationMs: number | null;
  key: string;
};

function latestTiming(message: AgentMessageView, steps: AgentProgressEvent[]): AgentMessageTiming | null {
  return message.timing ?? [...steps].reverse().find((step) => step.timing)?.timing ?? null;
}

export function AgentProgress({ message, steps, displayMode = "release" }: { message: AgentMessageView; steps: AgentProgressEvent[]; displayMode?: DisplayMode }) {
  const { t } = useI18n();
  const processing = message.status === "PROCESSING";
  const failed = message.status === "FAILED";
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
  const status = failed ? t("回答未完成") : processing ? displayMode === "debug" && currentPhase ? t(phases[currentPhase].label) : t("处理中") : t("已完成");
  const timingStatus = elapsedMs === null
    ? status
    : `${status} · ${processing ? t("已用") : t("总耗时")} ${formatDuration(elapsedMs)}`;

  // The mascot only accompanies live or failed answers; finished history stays quiet.
  const mood: MascotMood | null = failed ? "oops" : processing ? phaseMoods[currentPhase ?? "QUEUED"] : null;
  const mascot = mood ? <Mascot className={styles.progressMascot} mood={mood} size={32} /> : null;

  if (displayMode !== "debug") {
    return <div className={styles.progressCard} role="status" aria-label={t("回答状态")} data-live={Boolean(mascot)}>{mascot}<p className={failed ? styles.messageError : styles.progressNote}>{timingStatus}</p></div>;
  }

  return (
    <details className={styles.progressCard} open={processing}>
      <summary aria-label={t("回答过程")}>
        {mascot ?? <Check size={15} aria-hidden="true" />}
        <span>{t("回答过程")}</span><small className={failed ? styles.messageError : undefined}>{timingStatus}</small>
        <ChevronDown size={14} className={styles.progressChevron} aria-hidden="true" />
      </summary>
      {visibleSteps.length ? (
        <ol className={styles.progressSteps} aria-label={t("已同步的执行步骤")}>
          {visibleSteps.map((step, index) => {
            const phase = phases[step.phase];
            const latest = index === visibleSteps.length - 1;
            const state = latest && processing ? "active" : latest && failed ? "interrupted" : "done";
            const Icon = state === "active" ? LoaderCircle : phase.icon;
            return (
              <li key={step.key} data-state={state}>
                <div className={styles.stepIcon}><Icon size={17} className={state === "active" ? styles.spinning : undefined} aria-hidden="true" /></div>
                <div><div className={styles.stepHeading}><strong>{t(phase.label)}</strong><span>{state === "active" ? t("进行中") : state === "interrupted" ? t("未确认完成") : t("已执行")}{step.durationMs === null ? "" : ` · ${formatDuration(step.durationMs)}`}</span></div><p>{t(phase.detail)}</p></div>
              </li>
            );
          })}
        </ol>
      ) : <p className={styles.progressNote}>{processing ? t("正在同步执行状态…") : failed ? t("本次回答未完成。") : t("历史消息仅保留最终状态，完整执行步骤暂不可回放。")}</p>}
      {message.searchResult ? <p className={styles.progressNote}>{t("{videos} 个视频 · {clips} 段候选", { videos: message.searchResult.searchedVideoIds.length, clips: message.searchResult.hits.length })}{message.searchResult.partial ? t(" · 结果有资料限制") : ""}</p> : null}
    </details>
  );
}
