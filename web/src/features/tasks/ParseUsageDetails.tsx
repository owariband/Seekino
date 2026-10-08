import { useQuery } from "@tanstack/react-query";
import { ChevronDown, CircleAlert, Rows3 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { getUsageOverview, shouldRetryApiRequest, WORKSPACE_ID } from "../../api/client";
import type { ParseJobStatus } from "../../api/contracts";
import { formatMoneyUsd, formatTokens } from "../../shared/format";
import styles from "./tasks.module.css";

const operationLabels: Record<string, string> = {
  candidate_transcript: "候选对白识别",
  video_timeline: "视频时间线解析",
  neutral_visual_evidence: "视觉证据核验",
  fallback_video_timeline: "备用时间线整理",
  text_embedding: "文本向量化",
  video_embedding: "视频向量化",
};

export function ParseUsageDetails({ jobId, status }: { jobId: string; status: ParseJobStatus }) {
  const [expanded, setExpanded] = useState(false);
  const previousStatus = useRef(status);
  const terminal = status === "SUCCEEDED" || status === "FAILED";
  const usageQuery = useQuery({
    queryKey: ["usage-overview", WORKSPACE_ID, "run", jobId],
    queryFn: ({ signal }) => getUsageOverview({ runId: jobId }, signal),
    enabled: expanded,
    retry: shouldRetryApiRequest,
    staleTime: 30_000,
    refetchInterval: expanded ? 60_000 : false,
  });
  useEffect(() => {
    const previous = previousStatus.current;
    previousStatus.current = status;
    if (expanded && terminal && previous !== status) void usageQuery.refetch();
  }, [expanded, terminal, status, usageQuery.refetch]);
  const overview = usageQuery.data;
  const totals = overview?.status === "available" ? overview.totals : undefined;

  return (
    <details className={styles.usageDetails} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary><Rows3 size={14} />模型消耗<span>Langfuse 已同步记录 · USD</span><ChevronDown size={14} className={styles.usageChevron} /></summary>
      {expanded ? <div className={styles.usageBody}>
        {usageQuery.isPending ? <p role="status">正在读取模型消耗…</p> : null}
        {usageQuery.error instanceof Error ? <div className={styles.jobError} role="alert"><CircleAlert size={15} /><div><strong>模型消耗暂时无法读取</strong><p>{usageQuery.error.message}</p><button className="button-secondary" type="button" disabled={usageQuery.isFetching} onClick={() => void usageQuery.refetch()}>重新读取消耗</button></div></div> : null}
        {overview && !totals ? <p className={styles.usageNotice} role="status">{overview.status === "not_configured" ? "Langfuse 尚未配置，请在 .env.local 填写 LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY 和 LANGFUSE_BASE_URL。" : overview.status === "disabled" ? "Langfuse 用量采集已关闭。" : "Langfuse 暂时无法读取。"}{overview.message ? ` ${overview.message}` : ""}</p> : null}
        {overview && totals ? <>
          <p className={styles.usageSource}>{jobId} · 最近 30 天已同步记录</p>
          <dl className={styles.usageTotals} aria-label="本次任务模型消耗合计">
            <div><dt>{totals.totalTokens === null || overview.truncated ? "已知 Token 小计" : "已同步 Token"}</dt><dd>{formatTokens(totals.totalTokens ?? totals.knownTokens)}</dd></div>
            <div><dt>调用记录</dt><dd>{formatTokens(totals.callCount)}</dd></div>
            <div><dt>{totals.estimatedCostUsd === null || overview.truncated ? "已计价费用小计（USD）" : "预估费用（USD）"}</dt><dd>{formatMoneyUsd(totals.estimatedCostUsd ?? totals.knownCostUsd)}</dd></div>
          </dl>
          {overview.models.length ? <div className={styles.usageTableScroll}><table className={styles.usageTable}>
            <caption>本次任务已同步的模型调用</caption>
            <thead><tr><th>模型 / 服务商</th><th>职责</th><th>调用记录</th><th>输入 Token</th><th>输出 Token</th><th>缓存输入</th><th>缓存写入</th><th>推理 Token</th><th>总 Token</th><th>预估费用（USD）</th></tr></thead>
            <tbody>{overview.models.map((item) => <tr key={JSON.stringify([item.provider, item.model, item.category, item.operation])}>
              <th scope="row">{item.model ?? "未知模型"}<small>{item.provider ?? "未知服务商"}</small></th>
              <td>{item.category ? `${item.category === "embedding" ? "向量化" : item.category === "parse" ? "解析" : item.category} · ` : ""}{item.operation ? operationLabels[item.operation] ?? item.operation : "未知用途"}</td>
              <td>{formatTokens(item.callCount)}</td><td>{formatTokens(item.inputTokens)}</td><td>{formatTokens(item.outputTokens)}</td><td>{formatTokens(item.cachedInputTokens)}</td><td>{formatTokens(item.cacheWriteInputTokens)}</td><td>{formatTokens(item.reasoningTokens)}</td><td>{formatTokens(item.totalTokens)}</td><td>{formatMoneyUsd(item.estimatedCostUsd)}{item.missingUsageCount || item.unpricedCallCount ? <small>记录不完整</small> : null}</td>
            </tr>)}</tbody>
          </table></div> : <p className={styles.usageNotice}>当前窗口暂无已同步的模型调用。</p>}
          {totals.missingUsageCount ? <p className={styles.usageNotice}>{totals.missingUsageCount} 条调用记录缺少完整 Token 用量，显示已知小计。</p> : null}
          {totals.unpricedCallCount ? <p className={styles.usageNotice}>{totals.unpricedCallCount} 条调用记录尚未计价，费用不是完整总额。</p> : null}
          {overview.truncated ? <p className={styles.usageNotice}>当前读取结果被截断，不能代表完整消耗。</p> : null}
          <p className={styles.usageNotice}>费用由 Langfuse 以 USD 预估；新记录可能延迟同步，未记录的历史用量无法还原。</p>
        </> : null}
      </div> : null}
    </details>
  );
}
