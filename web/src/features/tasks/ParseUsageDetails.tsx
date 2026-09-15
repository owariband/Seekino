import { useQuery } from "@tanstack/react-query";
import { ChevronDown, CircleAlert, Rows3 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { getParseUsage, shouldRetryApiRequest, WORKSPACE_ID } from "../../api/client";
import type { ParseJobStatus } from "../../api/contracts";
import { episodeLabel } from "../../shared/format";
import styles from "./tasks.module.css";

const operationLabels: Record<string, string> = {
  candidate_transcript: "候选对白识别",
  video_timeline: "视频时间线解析",
  neutral_visual_evidence: "视觉证据核验",
  fallback_video_timeline: "备用时间线整理",
  text_embedding: "文本向量化",
  video_embedding: "视频向量化",
};

function tokens(value: number | null): string {
  return value === null ? "未记录" : value.toLocaleString("zh-CN");
}

function cost(value: string | null): string {
  if (value === null) return "未估算";
  const [integer, fraction = ""] = value.split(".");
  return `¥${integer!.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${fraction.padEnd(2, "0")}`;
}

export function ParseUsageDetails({ jobId, status }: { jobId: string; status: ParseJobStatus }) {
  const [expanded, setExpanded] = useState(false);
  const previousStatus = useRef(status);
  const terminal = status === "SUCCEEDED" || status === "FAILED";
  const usageQuery = useQuery({
    queryKey: ["parse-usage", WORKSPACE_ID, jobId],
    queryFn: () => getParseUsage(jobId),
    enabled: expanded,
    retry: shouldRetryApiRequest,
    refetchInterval: (query) => {
      if (!expanded) return false;
      if (!terminal) return 5_000;
      return query.state.data?.coverage === "partial" ? 15_000 : false;
    },
  });
  useEffect(() => {
    const previous = previousStatus.current;
    previousStatus.current = status;
    if (expanded && terminal && previous !== status) void usageQuery.refetch();
  }, [expanded, terminal, status, usageQuery.refetch]);
  const usage = usageQuery.data;

  return (
    <details className={styles.usageDetails} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary><Rows3 size={14} />模型消耗<span>解析与向量化 · 预估费用（¥）</span><ChevronDown size={14} className={styles.usageChevron} /></summary>
      {expanded ? <div className={styles.usageBody}>
        {usageQuery.isPending ? <p role="status">正在读取模型消耗…</p> : null}
        {usageQuery.error instanceof Error ? <div className={styles.jobError} role="alert"><CircleAlert size={15} /><div><strong>模型消耗暂时无法读取</strong><p>{usageQuery.error.message}</p><button className="button-secondary" type="button" disabled={usageQuery.isFetching} onClick={() => void usageQuery.refetch()}>重新读取消耗</button></div></div> : null}
        {usage ? <>
          <p className={styles.usageSource}>{usage.seriesName} · {episodeLabel(usage.episodeOrder)} · {usage.originalFilename}</p>
          <dl className={styles.usageTotals} aria-label="本次任务模型消耗合计">
            <div><dt>本次任务合计 · Token</dt><dd>{tokens(usage.totalTokens)}</dd></div>
            <div><dt>已记录调用</dt><dd>{usage.callCount.toLocaleString("zh-CN")}</dd></div>
            <div><dt>预估费用（人民币）</dt><dd>{cost(usage.estimatedCostCny)}</dd></div>
          </dl>
          {usage.items.length ? <div className={styles.usageTableScroll}><table className={styles.usageTable}>
            <caption>按模型与职责统计本次任务所有执行尝试</caption>
            <thead><tr><th>模型 / 服务商</th><th>职责</th><th>调用</th><th>输入 Token</th><th>输出 Token</th><th>缓存输入</th><th>推理 Token</th><th>总 Token</th><th>预估费用（¥）</th></tr></thead>
            <tbody>{usage.items.map((item) => <tr key={`${item.provider}:${item.model}:${item.category}:${item.operation}`}>
              <th scope="row">{item.model}<small>{item.provider}</small></th>
              <td>{item.category === "embedding" ? "向量化" : "解析"} · {operationLabels[item.operation] ?? item.operation}</td>
              <td>{item.callCount.toLocaleString("zh-CN")}</td><td>{tokens(item.inputTokens)}</td><td>{tokens(item.outputTokens)}</td><td>{tokens(item.cachedInputTokens)}</td><td>{tokens(item.reasoningTokens)}</td><td>{tokens(item.totalTokens)}</td><td>{cost(item.estimatedCostCny)}{item.missingUsageCount || item.unpricedCallCount ? <small>记录不完整</small> : null}</td>
            </tr>)}</tbody>
          </table></div> : <p className={styles.usageNotice}>{usage.coverage === "not_started" ? "任务尚未开始记录模型调用。" : usage.coverage === "recorded" ? "本次任务已跟踪，尚无模型调用。" : "暂无已记录的模型调用。"}</p>}
          {usage.coverage === "unknown" ? <p className={styles.usageNotice}>此任务没有模型消耗记录，无法还原历史 Token 和费用。</p> : null}
          {usage.coverage === "partial" ? <p className={styles.usageNotice}>{!terminal ? "模型消耗仍在采集中，已记录明细会持续更新。" : "部分执行尝试没有完整消耗记录；历史未记录用量不会计为零。"}</p> : null}
          {usage.missingUsageCount ? <p className={styles.usageNotice}>{usage.missingUsageCount} 次调用未返回完整 Token 用量，汇总可能不完整。</p> : null}
          {usage.unpricedCallCount ? <p className={styles.usageNotice}>{usage.unpricedCallCount} 次调用缺少完整计价信息，预估费用不完整。</p> : null}
          <p className={styles.usageNotice}>费用按已配置人民币单价预估，包含本次任务所有尝试中的解析与向量化调用。</p>
        </> : null}
      </div> : null}
    </details>
  );
}
