import { Check, ChevronDown, CircleAlert, Clock3, LoaderCircle, MessageSquareText, ScanSearch, ShieldCheck } from "lucide-react";

import type { AgentMessageView, AnswerPhase } from "../../api/contracts";
import type { AgentProgressEvent } from "./useAgentConversation";
import styles from "./search.module.css";

const phases: Record<AnswerPhase, { label: string; detail: string; icon: typeof Clock3 }> = {
  QUEUED: { label: "等待执行", detail: "服务端已接收请求。", icon: Clock3 },
  RETRIEVING: { label: "检索片段", detail: "查找与描述相关的视频证据。", icon: ScanSearch },
  EVALUATING: { label: "核验候选", detail: "检查候选片段是否符合描述。", icon: ShieldCheck },
  RESPONDING: { label: "生成回答", detail: "整理回答与可核验的视频证据。", icon: MessageSquareText },
};

export function AgentProgress({ message, steps }: { message: AgentMessageView; steps: AgentProgressEvent[] }) {
  const processing = message.status === "PROCESSING";
  const failed = message.status === "FAILED";
  const currentPhase = message.phase ?? steps.at(-1)?.phase;
  const visibleSteps = processing && message.phase && steps.at(-1)?.phase !== message.phase
    ? [...steps, { type: "answer.progress" as const, conversationId: message.conversationId, messageId: message.messageId, sequence: 0, occurredAt: message.updatedAt, phase: message.phase }]
    : steps;
  const status = failed ? "回答未完成" : processing ? currentPhase ? phases[currentPhase].label : "处理中" : "已完成";

  return (
    <details className={styles.progressCard} open={processing}>
      <summary aria-label="检索过程">
        {processing ? <LoaderCircle size={15} className={styles.spinning} aria-hidden="true" /> : failed ? <CircleAlert size={15} aria-hidden="true" /> : <Check size={15} aria-hidden="true" />}
        <span>检索过程</span><small className={failed ? styles.messageError : undefined}>{status}</small>
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
              <li key={step.sequence} data-state={state}>
                <div className={styles.stepIcon}><Icon size={17} className={state === "active" ? styles.spinning : undefined} aria-hidden="true" /></div>
                <div><div className={styles.stepHeading}><strong>{phase.label}</strong><span>{state === "active" ? "进行中" : state === "interrupted" ? "未确认完成" : "已执行"}</span></div><p>{phase.detail}</p></div>
              </li>
            );
          })}
        </ol>
      ) : <p className={styles.progressNote}>{processing ? "正在同步执行状态…" : failed ? "本次回答未完成。" : "历史消息仅保留最终状态，完整执行步骤暂不可回放。"}</p>}
      {message.searchResult ? <p className={styles.progressNote}>{message.searchResult.searchedVideoIds.length} 个视频 · {message.searchResult.hits.length} 段候选{message.searchResult.partial ? " · 结果有资料限制" : ""}</p> : null}
    </details>
  );
}
