import { Check, ChevronDown, CircleAlert, Clock3, LoaderCircle, MessageSquareText, ScanSearch, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";

import type { AgentMessageTiming, AgentMessageView, AnswerPhase } from "../../api/contracts";
import type { AgentProgressEvent } from "./useAgentConversation";
import styles from "./search.module.css";

const phases: Record<AnswerPhase, { label: string; detail: string; icon: typeof Clock3 }> = {
  QUEUED: { label: "等待执行", detail: "服务端已接收请求。", icon: Clock3 },
  RETRIEVING: { label: "检索片段", detail: "查找与描述相关的视频证据。", icon: ScanSearch },
  EVALUATING: { label: "核验候选", detail: "检查候选片段是否符合描述。", icon: ShieldCheck },
  RESPONDING: { label: "生成回答", detail: "整理回答与可核验的视频证据。", icon: MessageSquareText },
};

type VisibleStage = {
  phase: AnswerPhase;
  durationMs: number | null;
  key: string;
};

function formatDuration(durationMs: number): string {
  const milliseconds = Math.max(0, Math.round(durationMs));
  if (milliseconds < 1_000) return `${milliseconds} 毫秒`;
  if (milliseconds < 60_000) {
    const seconds = milliseconds / 1_000;
    return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  }
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.floor((milliseconds % 60_000) / 1_000);
  return `${minutes} 分 ${String(seconds).padStart(2, "0")} 秒`;
}

function latestTiming(message: AgentMessageView, steps: AgentProgressEvent[]): AgentMessageTiming | null {
  return message.timing ?? [...steps].reverse().find((step) => step.timing)?.timing ?? null;
}

export function AgentProgress({ message, steps }: { message: AgentMessageView; steps: AgentProgressEvent[] }) {
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
  const status = failed ? "回答未完成" : processing ? currentPhase ? phases[currentPhase].label : "处理中" : "已完成";
  const timingStatus = elapsedMs === null
    ? status
    : `${status} · ${processing ? "已用" : "总耗时"} ${formatDuration(elapsedMs)}`;

  return (
    <details className={styles.progressCard} open={processing}>
      <summary aria-label="检索过程">
        {processing ? <LoaderCircle size={15} className={styles.spinning} aria-hidden="true" /> : failed ? <CircleAlert size={15} aria-hidden="true" /> : <Check size={15} aria-hidden="true" />}
        <span>检索过程</span><small className={failed ? styles.messageError : undefined}>{timingStatus}</small>
        <ChevronDown size={14} className={styles.progressChevron} aria-hidden="true" />
      </summary>
      {visibleSteps.length ? (
        <ol className={styles.progressSteps} aria-label="已同步的执行步骤">
          {visibleSteps.map((step, index) => {
            const phase = phases[step.phase];
            const latest = index === visibleSteps.length - 1;
            const state = latest && processing ? "active" : latest && failed ? "interrupted" : "done";
            const Icon = state === "active" ? LoaderCircle : phase.icon;
            return (
              <li key={step.key} data-state={state}>
                <div className={styles.stepIcon}><Icon size={17} className={state === "active" ? styles.spinning : undefined} aria-hidden="true" /></div>
                <div><div className={styles.stepHeading}><strong>{phase.label}</strong><span>{state === "active" ? "进行中" : state === "interrupted" ? "未确认完成" : "已执行"}{step.durationMs === null ? "" : ` · ${formatDuration(step.durationMs)}`}</span></div><p>{phase.detail}</p></div>
              </li>
            );
          })}
        </ol>
      ) : <p className={styles.progressNote}>{processing ? "正在同步执行状态…" : failed ? "本次回答未完成。" : "历史消息仅保留最终状态，完整执行步骤暂不可回放。"}</p>}
      {message.searchResult ? <p className={styles.progressNote}>{message.searchResult.searchedVideoIds.length} 个视频 · {message.searchResult.hits.length} 段候选{message.searchResult.partial ? " · 结果有资料限制" : ""}</p> : null}
    </details>
  );
}
